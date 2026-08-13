from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
import zipfile
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from tests.plugin_signing import sign_plugin, trust_test_publisher

from repopilot_guard.api import _desktop_allowed_origins, _safe_task_interrupts, _safe_task_state, create_app
from repopilot_guard.config import ComponentCheck, RuntimeConfigurationManager
from repopilot_guard.context import ManagedDocumentStore
from repopilot_guard.intent_router import IntentRouter
from repopilot_guard.mcp import McpServerConfig, McpToolDescriptor
from repopilot_guard.mcp_runtime import (
    McpRawToolResult,
    McpRuntime,
    McpSessionInfo,
    McpSessionProtocol,
    McpToolDiscovery,
)
from repopilot_guard.plugins import PluginRegistry
from repopilot_guard.models import TaskRequest
from repopilot_guard.permissions import FULL_ACCESS_CONFIRMATION, PermissionGrant, PermissionMode
from repopilot_guard.project_registry import ProjectRegistry
from repopilot_guard.task_store import TaskStore
from repopilot_guard.workspace import WorkspaceManager


def _initialize_git_repository(repository: Path) -> None:
    """创建带基线提交的最小仓库，避免 API 测试伪造 `.git`。"""

    commands = (
        ("init", "-b", "main"),
        ("config", "user.name", "RepoPilot Test"),
        ("config", "user.email", "test@example.invalid"),
    )
    for arguments in commands:
        subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)
    (repository / "README.md").write_text("# fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=repository, check=True, capture_output=True)


class FakeRunner:
    def __init__(self, delay: float = 0.1) -> None:
        self.ran = False
        self.delay = delay
        self.requests: list[object] = []
        self.cancellation_requests: list[tuple[str, str | None]] = []
        self.result = SimpleNamespace(
            thread_id="thread-1", task_id="task-1", status="WAITING_APPROVAL", pending_approval=True, verdict=None,
            state={"tool_events": [{"type": "PLAN_GENERATED"}], "plan": {"summary": "计划"}, "verification_result": None, "error_summary": None, "git_diff": "", "context_snapshot": {"snapshot_sha256": "a" * 64, "included_chars": 123, "omitted_items": 0, "sources": [], "selected_skills": [], "bound_tool_ids": ["read_file"], "capability_ids": ["read_file"]}, "context_references": []},
            interrupts=({"type": "PLAN_APPROVAL_REQUIRED"},),
        )
        self.result.to_dict = lambda: {
            "thread_id": self.result.thread_id,
            "task_id": self.result.task_id,
            "status": self.result.status,
            "pending_approval": self.result.pending_approval,
            "verdict": self.result.verdict,
            "interrupts": list(self.result.interrupts),
            "state": self.result.state,
        }

    def run(self, request: object, thread_id: str | None, permission: object) -> object:
        # 模拟真实模型调用耗时，验证 HTTP 会先返回 RUNNING 而不是阻塞到图完成。
        time.sleep(self.delay)
        self.requests.append(request)
        self.result.state["task_operation"] = request.operation.value
        self.result.state["task_description"] = request.description
        self.ran = True
        return self.result

    def get(self, thread_id: str) -> object:
        if thread_id != "thread-1" or not self.ran:
            raise ValueError("NOT_FOUND")
        self.result.status = "REPORT"
        return self.result

    def resume(
        self,
        thread_id: str,
        approved: bool,
        *,
        decision: str | None = None,
        comment: str | None = None,
        selected_patch_paths: list[str] | None = None,
    ) -> object:
        self.result.pending_approval = False
        self.result.status = "REPORT"
        self.result.verdict = "BLOCKED" if not approved else "UNVERIFIED"
        return self.result

    def request_cancellation(self, thread_id: str, reason: str | None = None) -> None:
        self.cancellation_requests.append((thread_id, reason))


class MultiTurnFakeRunner:
    """为 API 多轮会话测试保存每个 thread 的独立图结果。"""

    def __init__(self) -> None:
        self.requests: list[object] = []
        self.results: dict[str, SimpleNamespace] = {}

    @staticmethod
    def _serialize(result: SimpleNamespace) -> dict[str, object]:
        return {
            "thread_id": result.thread_id,
            "task_id": result.task_id,
            "status": result.status,
            "pending_approval": result.pending_approval,
            "verdict": result.verdict,
            "interrupts": list(result.interrupts),
            "state": result.state,
        }

    def run(self, request: object, thread_id: str | None, permission: object) -> object:
        resolved_thread_id = str(thread_id)
        result = SimpleNamespace(
            thread_id=resolved_thread_id,
            task_id=request.task_id,
            status="WAITING_APPROVAL",
            pending_approval=True,
            verdict=None,
            state={
                "task_operation": request.operation.value,
                "task_description": request.description,
                "tool_events": [{"type": "PLAN_GENERATED"}],
                "plan": {"summary": f"已分析：{request.description}"},
                "verification_result": None,
                "error_summary": None,
                "git_diff": "",
            },
            interrupts=({"type": "PLAN_APPROVAL_REQUIRED"},),
        )
        result.to_dict = lambda item=result: self._serialize(item)
        self.requests.append(request)
        self.results[resolved_thread_id] = result
        return result

    def get(self, thread_id: str) -> object:
        try:
            return self.results[thread_id]
        except KeyError as error:
            raise ValueError("NOT_FOUND") from error

    def resume(
        self,
        thread_id: str,
        approved: bool,
        *,
        decision: str | None = None,
        comment: str | None = None,
    ) -> object:
        result = self.results[thread_id]
        accepted = decision == "approve" if decision is not None else approved is True
        result.pending_approval = False
        result.status = "REPORT"
        result.verdict = "UNVERIFIED" if accepted else "BLOCKED"
        result.interrupts = ()
        return result


class CheckpointThenFailingRunner(FakeRunner):
    """模拟 checkpoint 停在 PATCH 后，后台进程又发生未处理异常。"""

    def run(self, request: object, thread_id: str | None, permission: object) -> object:
        self.ran = True
        self.result.status = "PATCH"
        self.result.state["status"] = "PATCH"
        self.result.state["task_operation"] = request.operation.value
        self.result.state["task_description"] = request.description
        raise RuntimeError("不得返回给客户端的内部错误")


class FailedStatusRunner(FakeRunner):
    """返回终态 FAILED，验证 SSE 不会继续占用连接。"""

    def __init__(self) -> None:
        super().__init__(delay=0)
        self.get_calls = 0

    def get(self, thread_id: str) -> object:
        self.get_calls += 1
        self.result.status = "FAILED"
        self.result.pending_approval = False
        self.result.verdict = "FAILED"
        return self.result


class FakeApiMcpSession:
    async def initialize(self) -> McpSessionInfo:
        return McpSessionInfo("API Test MCP", "1.0", "2025-11-25", False)

    async def list_tools(self, server_name: str) -> McpToolDiscovery:
        return McpToolDiscovery(
            (
                McpToolDescriptor(
                    server_name,
                    "search",
                    "搜索文档。",
                    {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                ),
            )
        )

    async def call_tool(self, _name: str, arguments: dict[str, object]) -> McpRawToolResult:
        return McpRawToolResult(({"type": "text", "text": f"result:{arguments['query']}"},), None, False)

    async def ping(self) -> None:
        return None


class FakeApiMcpConnector:
    def __init__(self) -> None:
        self.opens = 0

    @asynccontextmanager
    async def open(
        self,
        _config: McpServerConfig,
        _environment: Mapping[str, str],
        _workspace_root: Path | None,
    ) -> AsyncIterator[McpSessionProtocol]:
        self.opens += 1
        yield FakeApiMcpSession()


class ApiTests(unittest.TestCase):
    def test_intent_route_api_returns_a_model_route_without_creating_a_task(self) -> None:
        class Router:
            def route(self, content: str, *, has_project: bool):
                self.content = content
                self.has_project = has_project
                return type(
                    "Route",
                    (),
                    {"to_dict": lambda _self: {"intent": "project_qa", "confidence": 0.96, "reason": "项目概览", "source": "model", "requires_confirmation": False}},
                )()

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "project"
            repository.mkdir()
            registry = ProjectRegistry(root / "state.sqlite")
            project = registry.add(repository, "路由项目")
            router = Router()
            try:
                with TestClient(create_app(FakeRunner(delay=0), registry, root / "runs", intent_router=router)) as client:
                    response = client.post("/api/intent-route", json={"content": "介绍这个项目", "project_id": project.project_id})
                    tasks = client.get("/api/tasks").json()["tasks"]

                self.assertEqual(200, response.status_code)
                self.assertEqual("project_qa", response.json()["route"]["intent"])
                self.assertTrue(router.has_project)
                self.assertEqual([], tasks)
            finally:
                registry.close()
    def test_retention_endpoints_preview_before_explicit_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output_root = root / "runs"
            store = TaskStore(root / "state.sqlite")
            registry = ProjectRegistry(root / "state.sqlite")
            try:
                store.create(
                    thread_id="thread-retention-api",
                    task_id="task-retention-api",
                    project_id=None,
                    repository=root / "repository",
                    output_root=output_root,
                    task_mode="safe-isolated",
                    permission_mode="safe",
                    workspace_mode="worktree",
                )
                store.sync_graph_result(
                    {
                        "thread_id": "thread-retention-api",
                        "status": "REPORT",
                        "pending_approval": False,
                        "verdict": "UNVERIFIED",
                        "state": {"tool_events": [{"type": "PLAN_GENERATED"}]},
                    }
                )
                store._connection.execute(
                    "UPDATE tasks SET updated_at = ? WHERE thread_id = ?",
                    ("2026-01-01T00:00:00+00:00", "thread-retention-api"),
                )
                store._connection.commit()
                with TestClient(create_app(FakeRunner(delay=0), registry, output_root, task_store=store)) as client:
                    preview = client.get("/api/tasks/retention-preview?older_than_days=0")
                    listed = client.get("/api/tasks")
                    unconfirmed = client.post("/api/tasks/archive-eligible", json={"confirmed": False})
                    archived = client.post("/api/tasks/archive-eligible", json={"older_than_days": 0, "confirmed": True})
                    archived_at = store.get("thread-retention-api").archived_at
                    artifact_kinds = {item.kind for item in store.artifacts("thread-retention-api")}

                self.assertEqual(200, preview.status_code)
                self.assertEqual(["thread-retention-api"], [item["thread_id"] for item in preview.json()["candidates"]])
                self.assertFalse(preview.json()["deletion_performed"])
                self.assertEqual(200, listed.status_code)
                self.assertNotIn("repository", listed.json()["tasks"][0])
                self.assertNotIn("output_root", listed.json()["tasks"][0])
                self.assertNotIn(str(root), listed.text)
                self.assertEqual(409, unconfirmed.status_code)
                self.assertEqual("TASK_RETENTION_CONFIRMATION_REQUIRED", unconfirmed.json()["detail"])
                self.assertEqual(200, archived.status_code)
                self.assertEqual(1, archived.json()["archived_count"])
                self.assertFalse(archived.json()["deletion_performed"])
                self.assertIsNotNone(archived_at)
                self.assertIn("report", artifact_kinds)
            finally:
                registry.close()

    def test_worktree_handoff_requires_full_confirmation_and_applies_only_to_clean_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repository"
            repository.mkdir()
            _initialize_git_repository(repository)
            output_root = root / "runs"
            request = TaskRequest(repository, "将已验证修复交接回 Local", output_root, task_id="task-1")
            prepared = WorkspaceManager().prepare(request, PermissionGrant(PermissionMode.SAFE))
            self.assertEqual("READY", prepared.status)
            assert prepared.workspace_path is not None
            (prepared.workspace_path / "README.md").write_text("# fixed\n", encoding="utf-8")
            store = TaskStore(root / "state.sqlite")
            runner = FakeRunner(delay=0)
            runner.ran = True
            runner.result.status = "REPORT"
            runner.result.pending_approval = False
            runner.result.state.update(
                {
                    "workspace_path": str(prepared.workspace_path),
                    "repository": str(repository),
                    "base_commit": prepared.base_commit,
                }
            )
            registry = ProjectRegistry(root / "state.sqlite")
            try:
                store.create(
                    thread_id="thread-1",
                    task_id="task-1",
                    project_id=None,
                    repository=repository,
                    output_root=output_root,
                    task_mode="safe-isolated",
                    permission_mode="safe",
                    workspace_mode="worktree",
                )
                store.sync_graph_result(runner.result.to_dict())
                with TestClient(create_app(runner, registry, output_root, task_store=store)) as client:
                    status_before = client.get("/api/tasks/thread-1/workspace")
                    missing_confirmation = client.post("/api/tasks/thread-1/workspace/handoff", json={})
                    source_before_handoff = (repository / "README.md").read_text(encoding="utf-8")
                    invalid_confirmation = client.post(
                        "/api/tasks/thread-1/workspace/handoff",
                        json={"confirmed": True, "confirmation": "我确认"},
                    )
                    handed_off = client.post(
                        "/api/tasks/thread-1/workspace/handoff",
                        json={"confirmed": True, "confirmation": FULL_ACCESS_CONFIRMATION},
                    )
                    status_after = client.get("/api/tasks/thread-1/workspace")
                    repeated = client.post(
                        "/api/tasks/thread-1/workspace/handoff",
                        json={"confirmed": True, "confirmation": FULL_ACCESS_CONFIRMATION},
                    )
                    events = [item.to_public_dict() for item in store.recent_events("thread-1", limit=8)]
                    source_after_handoff = (repository / "README.md").read_text(encoding="utf-8")
                    worktree_survived = prepared.workspace_path.is_dir()
            finally:
                registry.close()

        self.assertTrue(status_before.json()["local_handoff_available"])
        self.assertEqual("# fixture\n", source_before_handoff)
        self.assertEqual(409, missing_confirmation.status_code)
        self.assertEqual("LOCAL_HANDOFF_CONFIRMATION_REQUIRED", missing_confirmation.json()["detail"])
        self.assertEqual(409, invalid_confirmation.status_code)
        self.assertEqual("LOCAL_HANDOFF_FULL_CONFIRMATION_REQUIRED", invalid_confirmation.json()["detail"])
        self.assertEqual(200, handed_off.status_code)
        self.assertEqual("LOCAL_HANDOFF_APPLIED", handed_off.json()["code"])
        self.assertEqual("# fixed\n", source_after_handoff)
        self.assertTrue(worktree_survived)
        self.assertNotIn(str(repository), handed_off.text)
        self.assertFalse(status_after.json()["local_handoff_available"])
        self.assertEqual(409, repeated.status_code)
        self.assertEqual("LOCAL_HANDOFF_ALREADY_APPLIED", repeated.json()["detail"])
        event = next(item for item in events if item["type"] == "WORKSPACE_LOCAL_HANDOFF_APPLIED")
        self.assertEqual("USER_GRANTED_FULL_ACCESS", event["payload"]["audit_code"])
        self.assertNotIn(str(repository), json.dumps(event, ensure_ascii=False))

    def test_worktree_handoff_blocks_local_baseline_drift_without_overwriting_user_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repository"
            repository.mkdir()
            _initialize_git_repository(repository)
            output_root = root / "runs"
            request = TaskRequest(repository, "不要覆盖 Local 改动", output_root, task_id="task-1")
            prepared = WorkspaceManager().prepare(request, PermissionGrant(PermissionMode.SAFE))
            self.assertEqual("READY", prepared.status)
            assert prepared.workspace_path is not None
            (prepared.workspace_path / "README.md").write_text("# agent fix\n", encoding="utf-8")
            (repository / "README.md").write_text("# user edit\n", encoding="utf-8")
            store = TaskStore(root / "state.sqlite")
            runner = FakeRunner(delay=0)
            runner.ran = True
            runner.result.status = "REPORT"
            runner.result.pending_approval = False
            runner.result.state.update(
                {
                    "workspace_path": str(prepared.workspace_path),
                    "repository": str(repository),
                    "base_commit": prepared.base_commit,
                }
            )
            registry = ProjectRegistry(root / "state.sqlite")
            try:
                store.create(
                    thread_id="thread-1",
                    task_id="task-1",
                    project_id=None,
                    repository=repository,
                    output_root=output_root,
                    task_mode="safe-isolated",
                    permission_mode="safe",
                    workspace_mode="worktree",
                )
                store.sync_graph_result(runner.result.to_dict())
                with TestClient(create_app(runner, registry, output_root, task_store=store)) as client:
                    blocked = client.post(
                        "/api/tasks/thread-1/workspace/handoff",
                        json={"confirmed": True, "confirmation": FULL_ACCESS_CONFIRMATION},
                    )
                    source_after_block = (repository / "README.md").read_text(encoding="utf-8")
            finally:
                registry.close()

        self.assertEqual(409, blocked.status_code)
        self.assertEqual("LOCAL_BASELINE_CHANGED", blocked.json()["detail"])
        self.assertEqual("# user edit\n", source_after_block)

    def test_worktree_status_and_explicit_branch_creation_do_not_expose_local_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repository"
            repository.mkdir()
            _initialize_git_repository(repository)
            output_root = root / "runs"
            request = TaskRequest(repository, "将隔离修改转成分支", output_root, task_id="task-1")
            prepared = WorkspaceManager().prepare(request, PermissionGrant(PermissionMode.SAFE))
            self.assertEqual("READY", prepared.status)
            self.assertIsNotNone(prepared.workspace_path)
            store = TaskStore(root / "state.sqlite")
            runner = FakeRunner(delay=0)
            runner.ran = True
            runner.result.status = "REPORT"
            runner.result.pending_approval = False
            runner.result.state.update({"workspace_path": str(prepared.workspace_path)})
            registry = ProjectRegistry(root / "state.sqlite")
            try:
                store.create(
                    thread_id="thread-1",
                    task_id="task-1",
                    project_id=None,
                    repository=repository,
                    output_root=output_root,
                    task_mode="safe-isolated",
                    permission_mode="safe",
                    workspace_mode="worktree",
                )
                store.sync_graph_result(runner.result.to_dict())
                with TestClient(create_app(runner, registry, output_root, task_store=store)) as client:
                    status = client.get("/api/tasks/thread-1/workspace")
                    unconfirmed = client.post("/api/tasks/thread-1/workspace/branch", json={"branch": "repopilot/fix-order"})
                    created = client.post(
                        "/api/tasks/thread-1/workspace/branch",
                        json={"branch": "repopilot/fix-order", "confirmed": True},
                    )
                    repeated = client.post(
                        "/api/tasks/thread-1/workspace/branch",
                        json={"branch": "repopilot/second", "confirmed": True},
                    )
                    events = [item.to_public_dict() for item in store.recent_events("thread-1", limit=8)]
            finally:
                registry.close()

        self.assertEqual(200, status.status_code)
        self.assertEqual("detached", status.json()["lifecycle"])
        self.assertNotIn(str(prepared.workspace_path), status.text)
        self.assertEqual(409, unconfirmed.status_code)
        self.assertEqual("WORKSPACE_BRANCH_CONFIRMATION_REQUIRED", unconfirmed.json()["detail"])
        self.assertEqual(200, created.status_code)
        self.assertEqual("repopilot/fix-order", created.json()["branch"])
        self.assertEqual(409, repeated.status_code)
        event = next(item for item in events if item["type"] == "WORKSPACE_BRANCH_CREATED")
        self.assertEqual("repopilot/fix-order", event["payload"]["branch"])

    def test_approval_projection_keeps_patch_preview_but_drops_checkpoint_internals(self) -> None:
        raw_preview = {
            "status": "READY",
            "code": "PATCH_PREVIEW_READY",
            "paths": ["src/main/java/OrderService.java"],
            "diff": "- old\n+ new\n",
            "sha256": "a" * 64,
            "workspace_path": "D:/private/worktree",
        }
        interrupts = _safe_task_interrupts(
            [
                {
                    "type": "EXECUTION_APPROVAL_REQUIRED",
                    "message": "请审阅补丁。",
                    "candidate_files": ["src/main/java/OrderService.java"],
                    "recipe": "test",
                    "patch_preview": raw_preview,
                    "permission_confirmation": "我已了解完全权限风险",
                }
            ]
        )
        state = _safe_task_state({"patch_preview": raw_preview, "repository": "D:/private/repository"})

        self.assertEqual("PATCH_PREVIEW_READY", interrupts[0]["patch_preview"]["code"])
        self.assertEqual("- old\n+ new\n", interrupts[0]["patch_preview"]["diff"])
        self.assertNotIn("workspace_path", str(interrupts))
        self.assertNotIn("permission_confirmation", str(interrupts))
        self.assertNotIn("repository", state)
        self.assertEqual("a" * 64, state["patch_preview"]["sha256"])

    def test_execution_approval_projects_shell_preview_without_checkpoint_path_or_secret(self) -> None:
        preview = {
            "status": "READY",
            "argv": ["python", "-c", "print('token=visible-secret')"],
            "argv_sha256": "a" * 64,
            "approval_sha256": "b" * 64,
            "working_directory": "D:/private/worktree",
            "timeout_seconds": 30,
            "risk_categories": ["process", "write"],
            "internal": "must-not-leak",
        }

        interrupts = _safe_task_interrupts(
            [{"type": "EXECUTION_APPROVAL_REQUIRED", "shell_previews": [preview]}]
        )
        state = _safe_task_state({"shell_previews": [preview]})

        self.assertIn("token=[REDACTED]", interrupts[0]["shell_previews"][0]["argv"][2])
        self.assertNotIn("visible-secret", str(interrupts))
        self.assertNotIn("working_directory", str(interrupts))
        self.assertNotIn("internal", str(state))
        self.assertEqual("b" * 64, state["shell_previews"][0]["approval_sha256"])

    def test_plain_chat_persists_messages_without_creating_a_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            registry = ProjectRegistry(root / "state.sqlite")
            runner = FakeRunner(delay=0)
            try:
                with TestClient(
                    create_app(
                        runner,
                        registry,
                        root / "runs",
                        conversation_reply=lambda history, content, project: f"回复：{content}",
                    )
                ) as client:
                    created = client.post("/api/conversations", json={"display_title": "普通对话"})
                    self.assertEqual(200, created.status_code)
                    conversation_id = created.json()["conversation"]["conversation_id"]

                    response = client.post(
                        f"/api/conversations/{conversation_id}/chat",
                        json={"content": "你好"},
                    )

                    self.assertEqual(200, response.status_code)
                    self.assertEqual("READY", response.json()["status"])
                    self.assertEqual("回复：你好", response.json()["message"]["content"])
                    self.assertFalse(runner.ran)
                    self.assertEqual([], client.get("/api/tasks").json()["tasks"])
                    messages = client.get(
                        f"/api/conversations/{conversation_id}/messages"
                    ).json()["messages"]
                    self.assertEqual(
                        ["chat_request", "chat_response"],
                        [item["kind"] for item in messages],
                    )
            finally:
                registry.close()

    def test_plain_chat_streams_deltas_and_persists_final_response(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            registry = ProjectRegistry(root / "state.sqlite")
            runner = FakeRunner(delay=0)
            try:
                with TestClient(
                    create_app(
                        runner,
                        registry,
                        root / "runs",
                        conversation_reply_stream=lambda _history, _content, _project: iter(("实时", "回复")),
                    )
                ) as client:
                    created = client.post("/api/conversations", json={"display_title": "流式对话"})
                    conversation_id = created.json()["conversation"]["conversation_id"]

                    with client.stream(
                        "POST",
                        f"/api/conversations/{conversation_id}/chat/stream",
                        json={"content": "请实时回复"},
                    ) as response:
                        self.assertEqual(200, response.status_code)
                        payload = "".join(response.iter_text())

                    self.assertIn("event: message", payload)
                    self.assertIn('"content": "实时"', payload)
                    self.assertIn('"content": "回复"', payload)
                    self.assertIn("event: done", payload)
                    self.assertFalse(runner.ran)
                    messages = client.get(
                        f"/api/conversations/{conversation_id}/messages"
                    ).json()["messages"]
                    self.assertEqual(
                        ["chat_request", "chat_response"],
                        [item["kind"] for item in messages],
                    )
                    self.assertEqual("实时回复", messages[-1]["content"])
            finally:
                registry.close()

    def test_plain_chat_uses_controlled_document_attachment_without_running_a_task(self) -> None:
        observed_history: list[str] = []

        def reply_stream(history: str, _content: str, _project: str | None):
            observed_history.append(history)
            yield "已读取附件摘要"

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "non-git-project"
            repository.mkdir()
            source = root / "requirements.md"
            source.write_text("# 订单需求\n必须校验租户。\n", encoding="utf-8")
            registry = ProjectRegistry(root / "state.sqlite")
            project = registry.add(repository, "文档对话项目")
            document = ManagedDocumentStore(registry.database_path).import_document(
                source,
                project_id=project.project_id,
            )
            try:
                with TestClient(
                    create_app(
                        FakeRunner(delay=0),
                        registry,
                        root / "runs",
                        conversation_reply_stream=reply_stream,
                    )
                ) as client:
                    created = client.post("/api/conversations", json={"project_id": project.project_id})
                    conversation_id = created.json()["conversation"]["conversation_id"]
                    with client.stream(
                        "POST",
                        f"/api/conversations/{conversation_id}/chat/stream",
                        json={"content": "根据附件介绍需求", "attached_document_ids": [document.document_id]},
                    ) as response:
                        self.assertEqual(200, response.status_code)
                        self.assertIn("event: done", "".join(response.iter_text()))

                    self.assertIn("订单需求", observed_history[0])
                    self.assertIn("必须校验租户", observed_history[0])
                    self.assertNotIn(str(source), observed_history[0])
                    self.assertEqual([], client.get("/api/tasks").json()["tasks"])
            finally:
                registry.close()

    def test_plain_chat_attachment_requires_the_conversation_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "requirements.md"
            source.write_text("# 需求\n", encoding="utf-8")
            registry = ProjectRegistry(root / "state.sqlite")
            try:
                document = ManagedDocumentStore(registry.database_path).import_document(source, project_id="project-a")
                with TestClient(create_app(FakeRunner(delay=0), registry, root / "runs")) as client:
                    conversation_id = client.post("/api/conversations", json={}).json()["conversation"]["conversation_id"]
                    response = client.post(
                        f"/api/conversations/{conversation_id}/chat",
                        json={"content": "读取附件", "attached_document_ids": [document.document_id]},
                    )
                    self.assertEqual(409, response.status_code)
                    self.assertEqual("CHAT_ATTACHMENTS_REQUIRE_PROJECT", response.json()["detail"]["code"])
            finally:
                registry.close()

    def test_unassigned_chat_uses_first_message_title_and_can_branch_with_context(self) -> None:
        observed_history: list[str] = []

        def reply_stream(history: str, content: str, _project: str | None):
            observed_history.append(history)
            yield f"回复：{content}"

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            registry = ProjectRegistry(root / "state.sqlite")
            try:
                with TestClient(
                    create_app(
                        FakeRunner(delay=0),
                        registry,
                        root / "runs",
                        conversation_reply_stream=reply_stream,
                    )
                ) as client:
                    created = client.post("/api/conversations", json={})
                    self.assertEqual(200, created.status_code)
                    source_id = created.json()["conversation"]["conversation_id"]

                    with client.stream(
                        "POST",
                        f"/api/conversations/{source_id}/chat/stream",
                        json={"content": "介绍一下你能做什么"},
                    ) as response:
                        self.assertIn("event: done", "".join(response.iter_text()))

                    source = next(
                        item
                        for item in client.get("/api/conversations").json()["conversations"]
                        if item["conversation_id"] == source_id
                    )
                    self.assertEqual("介绍一下你能做什么", source["display_title"])
                    self.assertIsNone(source["project_id"])
                    source_messages = client.get(
                        f"/api/conversations/{source_id}/messages"
                    ).json()["messages"]

                    branched = client.post(
                        f"/api/conversations/{source_id}/branches",
                        json={"from_message_id": source_messages[-1]["message_id"]},
                    )
                    self.assertEqual(200, branched.status_code)
                    branch = branched.json()["conversation"]
                    self.assertEqual(source_id, branch["parent_conversation_id"])
                    self.assertEqual(2, branch["branched_from_sequence"])

                    with client.stream(
                        "POST",
                        f"/api/conversations/{branch['conversation_id']}/chat/stream",
                        json={"content": "在分支里继续说明"},
                    ) as response:
                        self.assertIn("event: done", "".join(response.iter_text()))

                    self.assertIn("介绍一下你能做什么", observed_history[-1])
                    self.assertIn("回复：介绍一下你能做什么", observed_history[-1])
                    self.assertNotIn(
                        "在分支里继续说明",
                        client.get(
                            f"/api/conversations/{source_id}/messages"
                        ).text,
                    )
            finally:
                registry.close()

    def test_plain_chat_stream_failure_does_not_persist_partial_answer_as_success(self) -> None:
        def interrupted_stream(_history: str, _content: str, _project: str | None):
            yield "未完成片段"
            raise RuntimeError("provider disconnected")

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            registry = ProjectRegistry(root / "state.sqlite")
            try:
                with TestClient(
                    create_app(
                        FakeRunner(delay=0),
                        registry,
                        root / "runs",
                        conversation_reply_stream=interrupted_stream,
                    )
                ) as client:
                    created = client.post("/api/conversations", json={"display_title": "中断测试"})
                    conversation_id = created.json()["conversation"]["conversation_id"]

                    with client.stream(
                        "POST",
                        f"/api/conversations/{conversation_id}/chat/stream",
                        json={"content": "触发中断"},
                    ) as response:
                        payload = "".join(response.iter_text())

                    self.assertIn("event: delta", payload)
                    self.assertIn("event: error", payload)
                    self.assertNotIn("event: done", payload)
                    self.assertNotIn("provider disconnected", payload)
                    messages = client.get(
                        f"/api/conversations/{conversation_id}/messages"
                    ).json()["messages"]
                    self.assertEqual(2, len(messages))
                    self.assertNotIn("未完成片段", messages[-1]["content"])
                    self.assertIn("中断", messages[-1]["content"])
            finally:
                registry.close()

    def test_chat_research_and_change_share_one_conversation_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repo"
            repository.mkdir()
            _initialize_git_repository(repository)
            registry = ProjectRegistry(root / "state.sqlite")
            project = registry.add(repository, "跨模式项目")
            runner = MultiTurnFakeRunner()
            try:
                with TestClient(
                    create_app(
                        runner,
                        registry,
                        root / "runs",
                        conversation_reply_stream=lambda _history, _content, _project: iter(("这是普通对话结论",)),
                    )
                ) as client:
                    created = client.post(
                        "/api/conversations",
                        json={"project_id": project.project_id, "display_title": "跨模式会话"},
                    )
                    conversation_id = created.json()["conversation"]["conversation_id"]
                    with client.stream(
                        "POST",
                        f"/api/conversations/{conversation_id}/chat/stream",
                        json={"content": "先记住订单模块背景"},
                    ) as response:
                        self.assertIn("event: done", "".join(response.iter_text()))

                    research = client.post(
                        "/api/tasks",
                        json={
                            "project_id": project.project_id,
                            "conversation_id": conversation_id,
                            "description": "分析订单查询链路",
                            "operation": "research",
                            "thread_id": "thread-cross-mode-research",
                        },
                    )
                    self.assertEqual(200, research.status_code)
                    deadline = time.monotonic() + 2
                    while len(runner.requests) < 1 and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertIn("先记住订单模块背景", runner.requests[0].conversation_context)
                    self.assertIn("这是普通对话结论", runner.requests[0].conversation_context)

                    client.post(
                        "/api/tasks/thread-cross-mode-research/approval",
                        json={"decision": "approve"},
                    )
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        messages = client.get(
                            f"/api/conversations/{conversation_id}/messages"
                        ).json()["messages"]
                        if any(item["kind"] == "task_summary" for item in messages):
                            break
                        time.sleep(0.01)

                    change = client.post(
                        "/api/tasks",
                        json={
                            "project_id": project.project_id,
                            "conversation_id": conversation_id,
                            "description": "根据刚才结论修改订单查询",
                            "operation": "change",
                            "thread_id": "thread-cross-mode-change",
                        },
                    )
                    self.assertEqual(200, change.status_code)
                    deadline = time.monotonic() + 2
                    while len(runner.requests) < 2 and time.monotonic() < deadline:
                        time.sleep(0.01)
                    inherited = runner.requests[1].conversation_context
                    self.assertIn("先记住订单模块背景", inherited)
                    self.assertIn("分析订单查询链路", inherited)
                    self.assertIn("这是普通对话结论", inherited)
            finally:
                registry.close()

    def test_sse_closes_immediately_for_failed_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repo"
            repository.mkdir()
            _initialize_git_repository(repository)
            registry = ProjectRegistry(root / "state.sqlite")
            project = registry.add(repository, "失败任务项目")
            task_store = TaskStore(root / "state.sqlite")
            runner = FailedStatusRunner()
            try:
                task_store.create(
                    thread_id="thread-failed-sse",
                    task_id="task-failed-sse",
                    project_id=project.project_id,
                    repository=repository,
                    output_root=root / "runs",
                    task_mode="safe-isolated",
                    task_operation="research",
                    permission_mode="safe",
                    workspace_mode="worktree",
                )
                task_store.sync_graph_result(
                    {
                        "thread_id": "thread-failed-sse",
                        "status": "FAILED",
                        "pending_approval": False,
                        "verdict": "FAILED",
                        "state": {},
                    }
                )
                with TestClient(create_app(runner, registry, root / "runs")) as client:
                    with patch("repopilot_guard.api.time.sleep", return_value=None):
                        stream = client.get("/api/tasks/thread-failed-sse/events")
                    self.assertEqual(200, stream.status_code)
                    self.assertEqual(1, runner.get_calls)
            finally:
                task_store.close()
                registry.close()

    def test_preview_origin_accepts_only_explicit_loopback_url(self) -> None:
        with patch.dict(os.environ, {"REPOPILOT_DESKTOP_PREVIEW_ORIGIN": "http://127.0.0.1:1427"}, clear=True):
            self.assertIn("http://127.0.0.1:1427", _desktop_allowed_origins())
        for invalid in ("https://127.0.0.1:1427", "http://example.com:1427", "http://127.0.0.1:1427/api"):
            with patch.dict(os.environ, {"REPOPILOT_DESKTOP_PREVIEW_ORIGIN": invalid}, clear=True):
                self.assertNotIn(invalid, _desktop_allowed_origins())

    def test_task_snapshot_exposes_sanitized_stage_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repo"
            repository.mkdir()
            _initialize_git_repository(repository)
            registry = ProjectRegistry(root / "state.sqlite")
            project = registry.add(repository, "进度快照项目")
            try:
                runner = FakeRunner(delay=0)
                runner.result.state["repository"] = "C:/private/repository"
                runner.result.state["permission_confirmation"] = "不应出现在任务详情中"
                with TestClient(create_app(runner, registry, root / "runs")) as client:
                    created = client.post(
                        "/api/tasks",
                        json={
                            "project_id": project.project_id,
                            "description": "生成只读研究计划",
                            "operation": "research",
                            "thread_id": "thread-1",
                        },
                    )
                    self.assertEqual(200, created.status_code)
                    deadline = time.monotonic() + 1
                    progress = None
                    while time.monotonic() < deadline:
                        snapshot = client.get("/api/tasks/thread-1")
                        self.assertEqual(200, snapshot.status_code)
                        progress = snapshot.json()["progress"]
                        if progress["current_stage"] == "plan_approval":
                            break
                        time.sleep(0.01)
                    self.assertIsNotNone(progress)
                    self.assertEqual("plan_approval", progress["current_stage"])
                    self.assertEqual("PENDING_APPROVAL", snapshot.json()["diagnostic"]["code"])
                    self.assertEqual("生成只读研究计划", snapshot.json()["state"]["task_description"])
                    self.assertNotIn("repository", snapshot.json()["state"])
                    self.assertNotIn("permission_confirmation", snapshot.json()["state"])
                    self.assertNotIn("tool_events", snapshot.json()["state"])
                    self.assertIsNone(progress["terminal_kind"])
                    self.assertEqual(
                        ["workspace", "preflight", "context", "research", "plan_approval", "report"],
                        [stage["id"] for stage in progress["stages"]],
                    )
                    self.assertNotIn("生成只读研究计划", json.dumps(progress, ensure_ascii=False))
            finally:
                registry.close()

    def test_runtime_configuration_persists_managed_values_without_echoing_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_file = root / "settings.env"
            config_file.write_text("# managed\nREPOPILOT_QDRANT_URL=http://127.0.0.1:6333\n", encoding="utf-8")
            registry = ProjectRegistry(root / "state.sqlite")
            environment = {
                "REPOPILOT_CONFIG_FILE": str(config_file),
                "REPOPILOT_DESKTOP_CONFIG_WRITE_ENABLED": "1",
            }
            try:
                with patch.dict(os.environ, environment, clear=True):
                    manager = RuntimeConfigurationManager()
                    with TestClient(
                        create_app(
                            FakeRunner(),
                            registry,
                            root / "runs",
                            runtime_configuration_manager=manager,
                        )
                    ) as client:
                        before = client.get("/api/runtime/configuration")
                        self.assertEqual(200, before.status_code)
                        self.assertTrue(before.json()["writable"])
                        self.assertFalse(before.json()["chat"]["api_key_configured"])

                        api_key = "desktop-secret-must-not-return"
                        saved = client.post(
                            "/api/runtime/configuration",
                            json={
                                "chat_base_url": "https://api.deepseek.com",
                                "chat_api_key": api_key,
                                "chat_model": "deepseek-chat",
                                "embedding_base_url": "https://embedding.example/v1",
                                "embedding_api_key": "embedding-secret-must-not-return",
                                "embedding_model": "embed-test",
                                "embedding_dimensions": 1536,
                                "user_skill_roots": "C:\\skills; D:\\team-skills",
                                "bundled_skill_roots": "D:\\RepoPilot\\skills",
                                "full_local_shell_enabled": True,
                            },
                        )

                        self.assertEqual(200, saved.status_code)
                        self.assertEqual("CONFIGURATION_SAVED", saved.json()["code"])
                        self.assertTrue(saved.json()["restart_required"])
                        self.assertTrue(saved.json()["chat"]["api_key_configured"])
                        self.assertTrue(saved.json()["experimental"]["full_local_shell_enabled"])
                        self.assertEqual(["C:\\skills", "D:\\team-skills"], saved.json()["skills"]["user_roots"])
                        self.assertEqual(["D:\\RepoPilot\\skills"], saved.json()["skills"]["bundled_roots"])
                        self.assertNotIn(api_key, json.dumps(saved.json(), ensure_ascii=False))
                        self.assertNotIn("embedding-secret-must-not-return", json.dumps(saved.json(), ensure_ascii=False))
                        listed = client.get("/api/runtime/configuration")
                        self.assertNotIn(api_key, json.dumps(listed.json(), ensure_ascii=False))
                        self.assertTrue(listed.json()["embedding"]["api_key_configured"])

                    persisted = config_file.read_text(encoding="utf-8")
                    self.assertIn('REPOPILOT_CHAT_API_KEY="desktop-secret-must-not-return"', persisted)
                    self.assertIn('REPOPILOT_EMBEDDING_DIMENSIONS="1536"', persisted)
                    self.assertIn('REPOPILOT_USER_SKILL_ROOTS="C:\\\\skills; D:\\\\team-skills"', persisted)
                    self.assertIn('REPOPILOT_FULL_LOCAL_SHELL_ENABLED="true"', persisted)
                    self.assertNotIn("\nREPOPILOT_UNTRUSTED=", persisted)
            finally:
                registry.close()

    def test_runtime_configuration_rejects_unmanaged_paths_and_newline_injection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            registry = ProjectRegistry(root / "state.sqlite")
            try:
                with TestClient(
                    create_app(
                        FakeRunner(),
                        registry,
                        root / "runs",
                        runtime_configuration_manager=RuntimeConfigurationManager(environment={}),
                    )
                ) as client:
                    blocked = client.post(
                        "/api/runtime/configuration",
                        json={"chat_model": "deepseek-chat"},
                    )
                    self.assertEqual(409, blocked.status_code)
                    self.assertEqual("CONFIGURATION_WRITE_NOT_MANAGED", blocked.json()["detail"]["code"])

                config_file = root / "settings.env"
                config_file.write_text("# managed\n", encoding="utf-8")
                environment = {
                    "REPOPILOT_CONFIG_FILE": str(config_file),
                    "REPOPILOT_DESKTOP_CONFIG_WRITE_ENABLED": "1",
                }
                with patch.dict(os.environ, environment, clear=True):
                    with TestClient(
                        create_app(
                            FakeRunner(),
                            registry,
                            root / "runs",
                            runtime_configuration_manager=RuntimeConfigurationManager(),
                        )
                    ) as client:
                        injected = client.post(
                            "/api/runtime/configuration",
                            json={"user_skill_roots": "C:\\skills\nREPOPILOT_UNTRUSTED=1"},
                        )
                    self.assertEqual(400, injected.status_code)
                    self.assertEqual("INVALID_CONFIGURATION_VALUE", injected.json()["detail"]["code"])
                self.assertEqual("# managed\n", config_file.read_text(encoding="utf-8"))
            finally:
                registry.close()

    def test_task_context_is_not_an_http_error_before_snapshot_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            registry = ProjectRegistry(root / "state.sqlite")
            runner = FakeRunner()
            runner.ran = True
            runner.result.status = "RUNNING"
            runner.result.state["context_snapshot"] = None
            try:
                with TestClient(create_app(runner, registry, root / "runs")) as client:
                    response = client.get("/api/tasks/thread-1/context")

                self.assertEqual(200, response.status_code)
                self.assertFalse(response.json()["available"])
                self.assertIsNone(response.json()["context_snapshot"])
                self.assertEqual([], response.json()["references"])
                self.assertEqual([], response.json()["attached_documents"])
            finally:
                registry.close()

    def test_task_context_exposes_attachment_metadata_without_paths_or_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            registry = ProjectRegistry(root / "state.sqlite")
            runner = FakeRunner()
            runner.ran = True
            document_id = "a" * 64
            content_sha256 = "b" * 64
            runner.result.state["attached_documents"] = [
                {
                    "document_id": document_id,
                    "display_name": "requirements.md",
                    "content_sha256": content_sha256,
                    "managed_path": "C:/private/repopilot/documents/requirements.md",
                    "content": "不得出现在任务上下文接口中",
                },
                {"document_id": "invalid", "display_name": "bad", "content_sha256": "bad"},
            ]
            try:
                with TestClient(create_app(runner, registry, root / "runs")) as client:
                    response = client.get("/api/tasks/thread-1/context")

                self.assertEqual(200, response.status_code)
                self.assertEqual(
                    [{"document_id": document_id, "display_name": "requirements.md", "content_sha256": content_sha256}],
                    response.json()["attached_documents"],
                )
                encoded = json.dumps(response.json(), ensure_ascii=False)
                self.assertNotIn("C:/private", encoded)
                self.assertNotIn("不得出现在任务上下文接口中", encoded)
            finally:
                registry.close()

    def test_project_diagnostics_exposes_mode_readiness_without_creating_a_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "non-git-project"
            repository.mkdir()
            (repository / "pom.xml").write_text("<project/>", encoding="utf-8")
            registry = ProjectRegistry(root / "state.sqlite")
            project = registry.add(repository, "诊断项目")
            try:
                with TestClient(create_app(FakeRunner(), registry, root / "runs")) as client:
                    response = client.get(f"/api/projects/{project.project_id}/diagnostics")

                payload = response.json()
                self.assertEqual(200, response.status_code)
                self.assertEqual("full-local", payload["recommended_task_mode"])
                self.assertEqual("change", payload["recommended_task_operation"])
                self.assertEqual("GIT_REPOSITORY_REQUIRED", payload["task_modes"]["safe_isolated"]["code"])
                self.assertEqual("FULL_LOCAL_FILE_SNAPSHOT_READY", payload["task_modes"]["full_local"]["code"])
                self.assertEqual(["change", "research"], payload["task_modes"]["full_local"]["allowed_operations"])
                self.assertEqual("JAVA_MAVEN_PROFILE_READY", payload["profiles"]["java_maven"]["code"])
                self.assertFalse((root / "runs").exists())
            finally:
                registry.close()

    def test_non_git_project_admits_full_local_change_with_file_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "non-git-project"
            repository.mkdir()
            registry = ProjectRegistry(root / "state.sqlite")
            project = registry.add(repository, "非 Git 项目")
            runner = FakeRunner(delay=0)
            try:
                with TestClient(create_app(runner, registry, root / "runs")) as client:
                    admitted_change = client.post(
                        "/api/tasks",
                        json={
                            "project_id": project.project_id,
                            "description": "直接修改代码",
                            "task_mode": "full-local",
                            "operation": "change",
                            "confirmation": FULL_ACCESS_CONFIRMATION,
                            "thread_id": "file-snapshot-change",
                        },
                    )

                    self.assertEqual(200, admitted_change.status_code)

                    deadline = time.monotonic() + 1
                    while not runner.ran and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(runner.ran)
                    self.assertEqual("change", runner.requests[0].operation.value)

                    empty_git = root / "empty-git-project"
                    empty_git.mkdir()
                    subprocess.run(
                        ["git", "init", "-b", "main"],
                        cwd=empty_git,
                        check=True,
                        capture_output=True,
                    )
                    empty_project = registry.add(empty_git, "空 Git 项目")
                    missing_baseline = client.post(
                        "/api/tasks",
                        json={
                            "project_id": empty_project.project_id,
                            "description": "直接修改代码",
                            "task_mode": "full-local",
                            "operation": "change",
                            "confirmation": FULL_ACCESS_CONFIRMATION,
                        },
                    )
                    self.assertEqual(200, missing_baseline.status_code)
            finally:
                registry.close()

    def test_health_keeps_api_ready_while_reporting_blocked_agent_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            registry = ProjectRegistry(root / "state.sqlite")
            try:
                blocked_qdrant = ComponentCheck(
                    component="qdrant",
                    ready=False,
                    code="QDRANT_UNAVAILABLE",
                    message="Qdrant 不可用。",
                )
                with TestClient(
                    create_app(
                        FakeRunner(),
                        registry,
                        root / "runs",
                        runtime_health_checks=lambda: (blocked_qdrant,),
                    )
                ) as client:
                    response = client.get("/api/health")

                self.assertEqual(200, response.status_code)
                self.assertEqual("READY", response.json()["status"])
                self.assertEqual("BLOCKED", response.json()["agent_status"])
                self.assertIn("task_evidence_export", response.json()["capabilities"])
                self.assertEqual("QDRANT_UNAVAILABLE", response.json()["dependencies"][0]["code"])
            finally:
                registry.close()

    def test_plugin_api_installs_lists_disables_and_blocks_tampered_enable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repo"
            repository.mkdir()
            plugin_root = root / "spring-maintenance"
            skill_root = plugin_root / "skills" / "java-review"
            skill_root.mkdir(parents=True)
            (plugin_root / "repopilot-plugin.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "id": "spring-maintenance",
                        "name": "Spring Maintenance",
                        "version": "1.0.0",
                        "description": "Java maintenance guidance.",
                        "skills_root": "skills",
                    }
                ),
                encoding="utf-8",
            )
            skill_file = skill_root / "SKILL.md"
            skill_file.write_text("---\nname: java-review\ndescription: review\n---\nRead code first.\n", encoding="utf-8")
            sign_plugin(plugin_root)
            registry = ProjectRegistry(root / "state.sqlite")
            plugin_registry = PluginRegistry(root / "state.sqlite")
            trust_test_publisher(plugin_registry)
            try:
                with TestClient(
                    create_app(
                        FakeRunner(),
                        registry,
                        root / "runs",
                        plugin_registry=plugin_registry,
                        shell_runtime_enabled=True,
                    )
                ) as client:
                    installed = client.post("/api/plugins", json={"source": str(plugin_root)})
                    self.assertEqual(200, installed.status_code)
                    self.assertTrue(installed.json()["plugin"]["active"])
                    original_hash = installed.json()["plugin"]["package_sha256"]

                    listed = client.get("/api/plugins")
                    self.assertEqual("spring-maintenance", listed.json()["plugins"][0]["plugin_id"])
                    self.assertEqual("VERIFIED", listed.json()["plugins"][0]["integrity_status"])
                    self.assertEqual("LOCAL_EXPLICIT", listed.json()["plugins"][0]["source_lock_status"])

                    disabled = client.post("/api/plugins/spring-maintenance/enabled", json={"enabled": False})
                    self.assertEqual(200, disabled.status_code)
                    self.assertFalse(disabled.json()["plugin"]["enabled"])

                    skill_file.write_text(skill_file.read_text(encoding="utf-8") + "Modified after review.\n", encoding="utf-8")
                    # 原目录改变不影响当前受控快照；用户必须显式重新安装才会产生新版本。
                    still_verified = client.get("/api/plugins")
                    self.assertEqual("VERIFIED", still_verified.json()["plugins"][0]["integrity_status"])
                    sign_plugin(plugin_root)
                    reinstalled = client.post("/api/plugins", json={"source": str(plugin_root)})
                    self.assertEqual(200, reinstalled.status_code)
                    self.assertNotEqual(original_hash, reinstalled.json()["plugin"]["package_sha256"])
                    versions = client.get("/api/plugins/spring-maintenance/versions")
                    self.assertEqual(200, versions.status_code)
                    self.assertEqual(2, len(versions.json()["versions"]))
                    rolled_back = client.post(
                        "/api/plugins/spring-maintenance/rollback",
                        json={"package_sha256": original_hash},
                    )
                    self.assertEqual(200, rolled_back.status_code)
                    self.assertEqual(original_hash, rolled_back.json()["plugin"]["package_sha256"])

                    snapshot_skill = plugin_registry.get("spring-maintenance").root_path / "skills" / "java-review" / "SKILL.md"
                    snapshot_skill.write_text(snapshot_skill.read_text(encoding="utf-8") + "Tampered snapshot.\n", encoding="utf-8")
                    blocked = client.post("/api/plugins/spring-maintenance/enabled", json={"enabled": True})
                    self.assertEqual(409, blocked.status_code)
                    self.assertEqual("PLUGIN_INTEGRITY_CHECK_FAILED", blocked.json()["detail"]["code"])

                    audit = client.get("/api/plugins/audit?plugin_id=spring-maintenance")
                    self.assertEqual(200, audit.status_code)
                    self.assertEqual("PLUGIN_ENABLE_BLOCKED", audit.json()["events"][0]["action"])
            finally:
                plugin_registry.close()
                registry.close()

    def test_plugin_trust_key_api_manages_public_keys_with_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            registry = ProjectRegistry(root / "state.sqlite")
            plugin_registry = PluginRegistry(root / "state.sqlite")
            try:
                with TestClient(
                    create_app(FakeRunner(), registry, root / "runs", plugin_registry=plugin_registry)
                ) as client:
                    created = client.post(
                        "/api/plugin-trust-keys",
                        json={"key_id": "test-publisher", "public_key_base64": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="},
                    )
                    self.assertEqual(200, created.status_code)
                    self.assertEqual("test-publisher", created.json()["trust_key"]["key_id"])
                    self.assertNotIn("private", json.dumps(created.json()).lower())
                    self.assertNotIn("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=", json.dumps(created.json()))

                    listed = client.get("/api/plugin-trust-keys")
                    self.assertEqual(200, listed.status_code)
                    self.assertEqual(["test-publisher"], [item["key_id"] for item in listed.json()["trust_keys"]])

                    removed = client.delete("/api/plugin-trust-keys/test-publisher")
                    self.assertEqual(200, removed.status_code)
                    self.assertEqual("PLUGIN_TRUST_KEY_REMOVED", removed.json()["code"])

                    audit = client.get("/api/plugin-trust-keys/audit")
                    self.assertEqual(200, audit.status_code)
                    self.assertEqual("PLUGIN_TRUST_KEY_REMOVED", audit.json()["events"][0]["action"])
            finally:
                plugin_registry.close()
                registry.close()

    def test_capability_directory_projects_policy_without_exposing_paths_or_skill_body(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repo"
            skill_root = repository / ".agents" / "skills" / "java-review"
            skill_root.mkdir(parents=True)
            (skill_root / "SKILL.md").write_text(
                "---\nname: java-review\ndescription: Java review guidance\nallowed-tools: [read_file]\n---\nNever expose this body.\n",
                encoding="utf-8",
            )
            user_skill_root = root / "user-skills"
            user_skill = user_skill_root / "java-team" / "SKILL.md"
            user_skill.parent.mkdir(parents=True)
            user_skill.write_text(
                "---\nname: java-team\ndescription: Shared Java guidance\nallowed-tools: [read_file]\n---\nNever expose this shared body.\n",
                encoding="utf-8",
            )
            plugin_root = root / "spring-maintenance"
            plugin_root.mkdir()
            (plugin_root / "repopilot-plugin.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "id": "spring-maintenance",
                        "name": "Spring Maintenance",
                        "version": "1.0.0",
                        "description": "Java maintenance guidance.",
                    }
                ),
                encoding="utf-8",
            )
            registry = ProjectRegistry(root / "state.sqlite")
            plugin_registry = PluginRegistry(root / "state.sqlite")
            project = registry.add(repository, "能力目录项目")
            try:
                with TestClient(
                    create_app(
                        FakeRunner(),
                        registry,
                        root / "runs",
                        plugin_registry=plugin_registry,
                        shell_runtime_enabled=True,
                        user_skill_roots=(user_skill_root,),
                    )
                ) as client:
                    self.assertEqual(200, client.post("/api/plugins", json={"source": str(plugin_root)}).status_code)
                    response = client.get(f"/api/projects/{project.project_id}/capability-directory")

                self.assertEqual(200, response.status_code)
                payload = response.json()
                builtin = next(item for item in payload["capabilities"] if item["capability_id"] == "read_file")
                skill = next(item for item in payload["capabilities"] if item["capability_id"] == "skill__java-review")
                user_skill = next(item for item in payload["capabilities"] if item["capability_id"] == "skill__java-team")
                shell = next(item for item in payload["capabilities"] if item["capability_id"] == "shell")
                self.assertEqual("RepoPilot 内置", builtin["source_label"])
                self.assertTrue(builtin["safe_policy"]["allowed"])
                self.assertEqual("当前项目", skill["source_label"])
                self.assertEqual("本机用户", user_skill["source_label"])
                self.assertEqual(["read_file"], skill["details"]["allowed_tools"])
                self.assertEqual("USER_GRANTED_FULL_ACCESS", skill["full_policy"]["code"])
                self.assertTrue(shell["requires_approval"])
                self.assertTrue(shell["full_policy"]["requires_approval"])
                self.assertEqual("Spring Maintenance", payload["plugins"][0]["name"])
                serialized = json.dumps(payload, ensure_ascii=False)
                self.assertNotIn(str(repository), serialized)
                self.assertNotIn(str(plugin_root), serialized)
                self.assertNotIn("Never expose this body", serialized)
                self.assertNotIn("Never expose this shared body", serialized)
            finally:
                plugin_registry.close()
                registry.close()

    def test_document_index_api_uses_controlled_service_and_preserves_blocked_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repo"
            repository.mkdir()
            registry = ProjectRegistry(root / "state.sqlite")
            project = registry.add(repository, "文档项目")
            original_document = root / "requirements.md"
            original_document.write_text("# 订单需求\n", encoding="utf-8")
            managed = ManagedDocumentStore(registry.database_path).import_document(
                original_document,
                project_id=project.project_id,
            )
            calls: list[tuple[str, Path]] = []

            def indexer(project_id: str, source: Path) -> dict[str, object]:
                calls.append((project_id, source))
                if source.name == "blocked.txt":
                    return {"status": "BLOCKED", "code": "DOCUMENT_UNREADABLE", "message": "文档不可读。"}
                return {
                    "status": "READY",
                    "code": "CONTEXT_INDEXED",
                    "indexed_chunks": 2,
                    "document": {"document_id": "document-1", "display_name": "requirements.md"},
                }

            try:
                with TestClient(create_app(FakeRunner(), registry, root / "runs", document_indexer=indexer)) as client:
                    listed = client.get(f"/api/projects/{project.project_id}/documents")
                    self.assertEqual(200, listed.status_code)
                    self.assertEqual(managed.document_id, listed.json()["documents"][0]["document_id"])
                    self.assertNotIn(str(original_document), json.dumps(listed.json(), ensure_ascii=False))
                    self.assertNotIn(str(managed.managed_path), json.dumps(listed.json(), ensure_ascii=False))

                    indexed = client.post(f"/api/projects/{project.project_id}/documents", json={"file": str(root / "requirements.md")})
                    self.assertEqual(200, indexed.status_code)
                    self.assertEqual("CONTEXT_INDEXED", indexed.json()["code"])
                    self.assertEqual([(project.project_id, root / "requirements.md")], calls)

                    blocked = client.post(f"/api/projects/{project.project_id}/documents", json={"file": str(root / "blocked.txt")})
                    self.assertEqual(409, blocked.status_code)
                    self.assertEqual("DOCUMENT_UNREADABLE", blocked.json()["detail"]["code"])
            finally:
                registry.close()

    def test_task_attachment_requires_current_project_controlled_document(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first_repository = root / "first-repo"
            second_repository = root / "second-repo"
            first_repository.mkdir()
            second_repository.mkdir()
            _initialize_git_repository(first_repository)
            _initialize_git_repository(second_repository)
            source = root / "outside-requirements.md"
            source.write_text("# 订单需求\n必须隔离租户。\n", encoding="utf-8")
            registry = ProjectRegistry(root / "state.sqlite")
            first = registry.add(first_repository, "项目一")
            second = registry.add(second_repository, "项目二")
            managed = ManagedDocumentStore(registry.database_path).import_document(source, project_id=first.project_id)
            runner = FakeRunner(delay=0)
            try:
                with TestClient(create_app(runner, registry, root / "runs")) as client:
                    created = client.post(
                        "/api/tasks",
                        json={
                            "project_id": first.project_id,
                            "description": "依据研发文档分析订单权限",
                            "operation": "research",
                            "thread_id": "attachment-thread",
                            "attached_document_ids": [managed.document_id],
                        },
                    )
                    self.assertEqual(200, created.status_code)
                    deadline = time.monotonic() + 1
                    while not runner.requests and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertEqual((managed.document_id,), runner.requests[0].attached_document_ids)
                    self.assertNotIn(str(source), json.dumps(created.json(), ensure_ascii=False))

                    cross_project = client.post(
                        "/api/tasks",
                        json={
                            "project_id": second.project_id,
                            "description": "错误绑定跨项目文档",
                            "operation": "research",
                            "attached_document_ids": [managed.document_id],
                        },
                    )
                    self.assertEqual(409, cross_project.status_code)
                    self.assertEqual("TASK_ATTACHMENT_NOT_FOUND", cross_project.json()["detail"]["code"])
                    self.assertNotIn(str(source), json.dumps(cross_project.json(), ensure_ascii=False))
            finally:
                registry.close()

    def test_project_mcp_api_requires_approval_calls_tool_and_blocks_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repo"
            config_dir = repository / ".repopilot"
            config_dir.mkdir(parents=True)
            (config_dir / "mcp.toml").write_text(
                "[[servers]]\n"
                'name="docs"\n'
                'transport="streamable_http"\n'
                'url="https://mcp.example.com/v1"\n'
                'allowed_tools=["search"]\n',
                encoding="utf-8",
            )
            registry = ProjectRegistry(root / "state.sqlite")
            project = registry.add(repository, "MCP 项目")
            connector = FakeApiMcpConnector()
            runtime_factory = lambda configuration, workspace: McpRuntime(
                configuration,
                connector=connector,
                workspace_root=workspace,
            )
            try:
                with TestClient(
                    create_app(
                        FakeRunner(),
                        registry,
                        root / "runs",
                        mcp_runtime_factory=runtime_factory,
                    )
                ) as client:
                    blocked = client.post(
                        f"/api/projects/{project.project_id}/mcp/probe",
                        json={"server": "docs"},
                    )
                    called = client.post(
                        f"/api/projects/{project.project_id}/mcp/call",
                        json={
                            "server": "docs",
                            "approve_risk": True,
                            "tool": "mcp__docs__search",
                            "arguments": {"query": "private-value"},
                        },
                    )
                    escaped = client.post(
                        f"/api/projects/{project.project_id}/mcp/probe",
                        json={"server": "docs", "config_path": "../outside.toml", "approve_risk": True},
                    )

                    self.assertEqual(409, blocked.status_code)
                    self.assertEqual("CAPABILITY_APPROVAL_REQUIRED", blocked.json()["detail"]["code"])
                    self.assertEqual(200, called.status_code)
                    self.assertEqual("MCP_TOOL_COMPLETED", called.json()["code"])
                    self.assertEqual("CLOSED", called.json()["closed"]["state"])
                    self.assertNotIn("private-value", str(called.json()["events"]))
                    self.assertEqual(409, escaped.status_code)
                    self.assertEqual("MCP_CONFIG_PATH_ESCAPE", escaped.json()["detail"]["code"])
                    self.assertEqual(1, connector.opens)
            finally:
                registry.close()

    def test_project_mcp_probe_accepts_only_active_verified_plugin_snapshot_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repo"
            repository.mkdir()
            plugin_root = root / "docs-plugin"
            plugin_root.mkdir()
            (plugin_root / "repopilot-plugin.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "id": "docs-plugin",
                        "name": "研发文档插件",
                        "version": "1.0.0",
                        "description": "提供只读研发文档工具。",
                        "mcp_config": "mcp.toml",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (plugin_root / "mcp.toml").write_text(
                "[[servers]]\n"
                'name="docs"\n'
                'transport="streamable_http"\n'
                'url="https://mcp.example.com/v1"\n'
                'access="read_only"\n'
                'allowed_tools=["search"]\n',
                encoding="utf-8",
            )
            sign_plugin(plugin_root)
            registry = ProjectRegistry(root / "state.sqlite")
            plugin_registry = PluginRegistry(root / "state.sqlite")
            trust_test_publisher(plugin_registry)
            project = registry.add(repository, "插件 MCP 项目")
            connector = FakeApiMcpConnector()
            runtime_factory = lambda configuration, workspace: McpRuntime(
                configuration,
                connector=connector,
                workspace_root=workspace,
            )
            try:
                plugin_registry.install(plugin_root)
                with TestClient(
                    create_app(
                        FakeRunner(),
                        registry,
                        root / "runs",
                        mcp_runtime_factory=runtime_factory,
                        plugin_registry=plugin_registry,
                    )
                ) as client:
                    probe = client.post(
                        f"/api/projects/{project.project_id}/mcp/probe",
                        json={"server": "docs", "config_source": "plugin:docs-plugin", "approve_risk": True},
                    )
                    self.assertEqual(200, probe.status_code)
                    self.assertEqual("plugin:docs-plugin", probe.json()["config_source"])
                    self.assertNotIn(str(plugin_root), json.dumps(probe.json(), ensure_ascii=False))

                    plugin_registry.disable("docs-plugin")
                    blocked = client.post(
                        f"/api/projects/{project.project_id}/mcp/probe",
                        json={"server": "docs", "config_source": "plugin:docs-plugin", "approve_risk": True},
                    )
                    self.assertEqual(409, blocked.status_code)
                    self.assertEqual("MCP_PLUGIN_SOURCE_UNAVAILABLE", blocked.json()["detail"]["code"])
            finally:
                plugin_registry.close()
                registry.close()

    def test_local_api_creates_safe_task_and_streams_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repo"
            repository.mkdir()
            _initialize_git_repository(repository)
            registry = ProjectRegistry(root / "state.sqlite")
            project = registry.add(repository, "演示项目")
            try:
                runner = FakeRunner()
                with TestClient(create_app(runner, registry, root / "runs")) as client:
                    self.assertEqual(200, client.get("/api/health").status_code)
                    preflight = client.options(
                        "/api/projects",
                        headers={"Origin": "http://127.0.0.1:1420", "Access-Control-Request-Method": "GET"},
                    )
                    self.assertEqual("http://127.0.0.1:1420", preflight.headers["access-control-allow-origin"])
                    tauri_preflight = client.options(
                        "/api/projects",
                        headers={"Origin": "http://tauri.localhost", "Access-Control-Request-Method": "GET"},
                    )
                    self.assertEqual("http://tauri.localhost", tauri_preflight.headers["access-control-allow-origin"])
                    task = client.post(
                        "/api/tasks",
                        json={
                            "project_id": project.project_id,
                            "description": "分析问题",
                            "task_mode": "safe-isolated",
                            "operation": "research",
                            "thread_id": "thread-1",
                            "approved_mcp_tools": ["mcp__docs__search"],
                        },
                    )
                    self.assertEqual(200, task.status_code)
                    self.assertEqual("RUNNING", task.json()["status"])
                    self.assertEqual("分析问题", task.json()["display_title"])
                    self.assertEqual("research", task.json()["task_operation"])
                    self.assertEqual("分析问题", task.json()["task_description"])
                    self.assertTrue(task.json()["trace_id"].startswith("trace-"))
                    time.sleep(0.15)
                    self.assertEqual(("mcp__docs__search",), runner.requests[0].approved_mcp_tools)
                    self.assertEqual("research", runner.requests[0].operation.value)
                    detail = client.get("/api/tasks/thread-1").json()
                    self.assertEqual("research", detail["task_operation"])
                    self.assertEqual("分析问题", detail["task_description"])
                    listed_tasks = client.get("/api/tasks").json()["tasks"]
                    self.assertEqual(1, len(listed_tasks))
                    self.assertEqual("分析问题", listed_tasks[0]["display_title"])
                    time.sleep(0.05)
                    stream = client.get("/api/tasks/thread-1/events")
                    self.assertIn("PLAN_GENERATED", stream.text)
                    self.assertIn("id: thread-1:", stream.text)
                    self.assertIn(task.json()["trace_id"], stream.text)
                    artifacts = client.get("/api/tasks/thread-1/artifacts")
                    self.assertEqual(200, artifacts.status_code)
                    self.assertIn("plan_json", {item["kind"] for item in artifacts.json()["artifacts"]})
                    plan = client.get("/api/tasks/thread-1/artifacts/plan_json")
                    versions = client.get("/api/tasks/thread-1/artifacts/plan_json/versions")
                    first_version = client.get("/api/tasks/thread-1/artifacts/plan_json/versions/1")
                    context = client.get("/api/tasks/thread-1/context")
                    telemetry = client.get("/api/tasks/thread-1/telemetry")
                    self.assertEqual(200, plan.status_code)
                    self.assertIn("计划", plan.json()["content"])
                    self.assertEqual(200, versions.status_code)
                    self.assertEqual([1], [item["version"] for item in versions.json()["versions"]])
                    self.assertEqual(200, first_version.status_code)
                    self.assertIn("计划", first_version.json()["content"])
                    self.assertEqual(200, context.status_code)
                    self.assertEqual(["read_file"], context.json()["context_snapshot"]["bound_tool_ids"])
                    self.assertEqual(200, telemetry.status_code)
                    self.assertEqual(0, telemetry.json()["model"]["total_tokens"])
            finally:
                registry.close()

    def test_task_capability_requires_feature_flag_and_full_local_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repo"
            repository.mkdir()
            _initialize_git_repository(repository)
            registry = ProjectRegistry(root / "state.sqlite")
            project = registry.add(repository, "Shell 授权项目")
            try:
                runner = FakeRunner(delay=0)
                with TestClient(
                    create_app(runner, registry, root / "runs", shell_runtime_enabled=True)
                ) as client:
                    safe = client.post(
                        "/api/tasks",
                        json={
                            "project_id": project.project_id,
                            "description": "仅分析 Git 状态",
                            "task_mode": "safe-isolated",
                            "operation": "research",
                            "approved_capabilities": ["shell"],
                        },
                    )
                    self.assertEqual(409, safe.status_code)
                    self.assertEqual("SHELL_REQUIRES_FULL_LOCAL", safe.json()["detail"]["code"])

                    accepted = client.post(
                        "/api/tasks",
                        json={
                            "project_id": project.project_id,
                            "description": "仅分析 Git 状态",
                            "task_mode": "full-local",
                            "operation": "research",
                            "confirmation": FULL_ACCESS_CONFIRMATION,
                            "thread_id": "thread-shell",
                            "approved_capabilities": ["shell"],
                        },
                    )
                    self.assertEqual(200, accepted.status_code)
                    time.sleep(0.05)
                    self.assertEqual(("shell",), runner.requests[0].approved_capabilities)

                disabled_runner = FakeRunner(delay=0)
                with TestClient(create_app(disabled_runner, registry, root / "runs")) as client:
                    disabled = client.post(
                        "/api/tasks",
                        json={
                            "project_id": project.project_id,
                            "description": "仅分析 Git 状态",
                            "task_mode": "full-local",
                            "operation": "research",
                            "confirmation": FULL_ACCESS_CONFIRMATION,
                            "approved_capabilities": ["shell"],
                        },
                    )
                    self.assertEqual(409, disabled.status_code)
                    self.assertEqual("CAPABILITY_NOT_AVAILABLE", disabled.json()["detail"]["code"])
            finally:
                registry.close()

    def test_final_task_evidence_export_uses_server_side_integrity_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repo"
            repository.mkdir()
            _initialize_git_repository(repository)
            registry = ProjectRegistry(root / "state.sqlite")
            project = registry.add(repository, "导出项目")
            try:
                runner = FakeRunner()
                runner.result.status = "REPORT"
                runner.result.pending_approval = False
                runner.result.verdict = "UNVERIFIED"
                target = root / "exports" / "task-evidence.zip"
                with TestClient(create_app(runner, registry, root / "runs")) as client:
                    created = client.post(
                        "/api/tasks",
                        json={
                            "project_id": project.project_id,
                            "description": "导出审计包",
                            "task_mode": "safe-isolated",
                            "operation": "research",
                            "thread_id": "thread-1",
                        },
                    )
                    self.assertEqual(200, created.status_code)
                    for _ in range(20):
                        snapshot = client.get("/api/tasks/thread-1")
                        if snapshot.json().get("status") == "REPORT":
                            break
                        time.sleep(0.02)
                    self.assertEqual("REPORT", snapshot.json()["status"])

                    exported = client.post(
                        "/api/tasks/thread-1/export",
                        json={"output": str(target)},
                    )
                    self.assertEqual(200, exported.status_code)
                    self.assertTrue(target.is_file())
                    self.assertGreater(exported.json()["export"]["artifact_count"], 0)
                    with zipfile.ZipFile(target) as archive:
                        self.assertIn("manifest.json", archive.namelist())
                        self.assertIn("evidence.jsonl", archive.namelist())
                        self.assertNotIn(str(repository), archive.read("manifest.json").decode("utf-8"))

                    duplicate = client.post(
                        "/api/tasks/thread-1/export",
                        json={"output": str(target)},
                    )
                    relative = client.post(
                        "/api/tasks/thread-1/export",
                        json={"output": "task-evidence.zip"},
                    )
                    self.assertEqual(409, duplicate.status_code)
                    self.assertEqual("TASK_EXPORT_OUTPUT_EXISTS", duplicate.json()["detail"])
                    self.assertEqual(422, relative.status_code)
                    self.assertEqual("TASK_EXPORT_OUTPUT_MUST_BE_ABSOLUTE", relative.json()["detail"])
            finally:
                registry.close()

    def test_api_cancels_background_task_then_archives_it_without_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repo"
            repository.mkdir()
            _initialize_git_repository(repository)
            registry = ProjectRegistry(root / "state.sqlite")
            project = registry.add(repository, "演示项目")
            try:
                runner = FakeRunner(delay=0.3)
                with TestClient(create_app(runner, registry, root / "runs")) as client:
                    created = client.post("/api/tasks", json={"project_id": project.project_id, "description": "可取消任务", "thread_id": "thread-1"})
                    self.assertEqual(200, created.status_code)
                    cancelled = client.post("/api/tasks/thread-1/cancel", json={"reason": "用户停止"})
                    self.assertEqual(200, cancelled.status_code)
                    self.assertIn(cancelled.json()["status"], {"CANCELLATION_REQUESTED", "CANCELLED"})
                    self.assertEqual([("thread-1", "用户停止")], runner.cancellation_requests)
                    time.sleep(0.4)
                    final = client.get("/api/tasks/thread-1")
                    self.assertEqual("CANCELLED", final.json()["status"])
                    archived = client.delete("/api/tasks/thread-1")
                    self.assertEqual(200, archived.status_code)
                    self.assertEqual([], client.get("/api/tasks").json()["tasks"])
                    history = client.get("/api/tasks?include_archived=true").json()["tasks"]
                    self.assertEqual("thread-1", history[0]["thread_id"])
                    events = client.get("/api/tasks/thread-1/events")
                    self.assertIn("TASK_CANCELLED", events.text)
                    self.assertIn("TASK_ARCHIVED", events.text)
            finally:
                registry.close()

    def test_runtime_failure_overrides_stale_graph_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repo"
            repository.mkdir()
            _initialize_git_repository(repository)
            registry = ProjectRegistry(root / "state.sqlite")
            project = registry.add(repository, "失败恢复项目")
            try:
                runner = CheckpointThenFailingRunner()
                with TestClient(create_app(runner, registry, root / "runs")) as client:
                    created = client.post(
                        "/api/tasks",
                        json={
                            "project_id": project.project_id,
                            "description": "验证运行时失败状态",
                            "thread_id": "thread-1",
                        },
                    )
                    self.assertEqual(200, created.status_code)

                    deadline = time.monotonic() + 2
                    snapshot = client.get("/api/tasks/thread-1").json()
                    while snapshot["status"] != "BLOCKED" and time.monotonic() < deadline:
                        time.sleep(0.02)
                        snapshot = client.get("/api/tasks/thread-1").json()

                    connection = sqlite3.connect(registry.database_path)
                    try:
                        connection.execute(
                            "UPDATE tasks SET display_title = NULL WHERE thread_id = ?",
                            ("thread-1",),
                        )
                        connection.commit()
                    finally:
                        connection.close()
                    snapshot = client.get("/api/tasks/thread-1").json()

                    self.assertEqual("BLOCKED", snapshot["status"])
                    self.assertEqual("BLOCKED", snapshot["verdict"])
                    self.assertEqual("TASK_RUNTIME_FAILED: RuntimeError", snapshot["error_summary"])
                    self.assertEqual("change", snapshot["task_operation"])
                    self.assertEqual("验证运行时失败状态", snapshot["task_description"])
                    self.assertEqual("验证运行时失败状态", snapshot["display_title"])
                    self.assertNotIn("不得返回给客户端的内部错误", json.dumps(snapshot, ensure_ascii=False))
            finally:
                registry.close()

    def test_conversation_runs_multiple_tasks_and_injects_prior_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repo"
            repository.mkdir()
            _initialize_git_repository(repository)
            registry = ProjectRegistry(root / "state.sqlite")
            project = registry.add(repository, "多轮任务项目")
            runner = MultiTurnFakeRunner()
            try:
                with TestClient(create_app(runner, registry, root / "runs")) as client:
                    created_conversation = client.post(
                        "/api/conversations",
                        json={
                            "project_id": project.project_id,
                            "display_title": "连续排查订单模块",
                            "mode": "plan",
                        },
                    )
                    self.assertEqual(200, created_conversation.status_code)
                    conversation_id = created_conversation.json()["conversation"]["conversation_id"]

                    first = client.post(
                        "/api/tasks",
                        json={
                            "project_id": project.project_id,
                            "conversation_id": conversation_id,
                            "description": "先梳理订单模块结构",
                            "operation": "research",
                            "thread_id": "thread-conversation-first",
                        },
                    )
                    self.assertEqual(200, first.status_code)
                    self.assertEqual(conversation_id, first.json()["conversation_id"])

                    deadline = time.monotonic() + 2
                    first_snapshot: dict[str, object] = {}
                    while time.monotonic() < deadline:
                        first_snapshot = client.get(
                            "/api/tasks/thread-conversation-first"
                        ).json()
                        if first_snapshot.get("pending_approval"):
                            break
                        time.sleep(0.01)
                    self.assertTrue(first_snapshot.get("pending_approval"))
                    self.assertEqual("", runner.requests[0].conversation_context)

                    messages = client.get(
                        f"/api/conversations/{conversation_id}/messages"
                    ).json()["messages"]
                    self.assertEqual(["task_request"], [item["kind"] for item in messages])
                    self.assertEqual("先梳理订单模块结构", messages[0]["content"])

                    concurrent = client.post(
                        "/api/tasks",
                        json={
                            "project_id": project.project_id,
                            "conversation_id": conversation_id,
                            "description": "不应并行创建",
                            "operation": "research",
                            "thread_id": "thread-conversation-concurrent",
                        },
                    )
                    self.assertEqual(409, concurrent.status_code)
                    self.assertEqual("CONVERSATION_TASK_RUNNING", concurrent.json()["detail"])

                    approved = client.post(
                        "/api/tasks/thread-conversation-first/approval",
                        json={"decision": "approve"},
                    )
                    self.assertEqual(200, approved.status_code)

                    deadline = time.monotonic() + 2
                    first_messages: list[dict[str, object]] = []
                    while time.monotonic() < deadline:
                        response = client.get(
                            f"/api/conversations/{conversation_id}/messages"
                        )
                        self.assertEqual(200, response.status_code)
                        first_messages = response.json()["messages"]
                        if len(first_messages) == 2:
                            break
                        time.sleep(0.01)
                    self.assertEqual(["user", "assistant"], [item["role"] for item in first_messages])
                    self.assertIn("处理总结", first_messages[-1]["content"])
                    self.assertIn("已分析：先梳理订单模块结构", first_messages[-1]["content"])

                    second = client.post(
                        "/api/tasks",
                        json={
                            "project_id": project.project_id,
                            "conversation_id": conversation_id,
                            "description": "继续定位订单查询入口",
                            "operation": "research",
                            "thread_id": "thread-conversation-second",
                        },
                    )
                    self.assertEqual(200, second.status_code)

                    deadline = time.monotonic() + 2
                    while len(runner.requests) < 2 and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertEqual(2, len(runner.requests))
                    inherited_context = runner.requests[-1].conversation_context
                    self.assertIn("先梳理订单模块结构", inherited_context)
                    self.assertIn("已分析：先梳理订单模块结构", inherited_context)
                    self.assertIn("不可信上下文", inherited_context)

                    all_messages = client.get(
                        f"/api/conversations/{conversation_id}/messages"
                    ).json()["messages"]
                    self.assertEqual(3, len(all_messages))
                    self.assertEqual("继续定位订单查询入口", all_messages[-1]["content"])
            finally:
                registry.close()

    def test_api_manages_project_conversation_and_task_titles_without_deleting_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repo"
            repository.mkdir()
            _initialize_git_repository(repository)
            registry = ProjectRegistry(root / "state.sqlite")
            project = registry.add(repository, "旧项目名")
            try:
                with TestClient(create_app(FakeRunner(delay=0), registry, root / "runs")) as client:
                    renamed_project = client.patch(
                        f"/api/projects/{project.project_id}",
                        json={"display_name": "订单服务"},
                    )
                    self.assertEqual(200, renamed_project.status_code)
                    self.assertEqual("订单服务", renamed_project.json()["project"]["display_name"])

                    draft = client.post(
                        "/api/conversations",
                        json={"display_title": "先讨论接口边界", "mode": "plan"},
                    )
                    self.assertEqual(200, draft.status_code)
                    conversation_id = draft.json()["conversation"]["conversation_id"]
                    self.assertIsNone(draft.json()["conversation"]["project_id"])

                    attached = client.patch(
                        f"/api/conversations/{conversation_id}",
                        json={"project_id": project.project_id, "display_title": "订单接口计划"},
                    )
                    self.assertEqual(200, attached.status_code)
                    self.assertEqual(project.project_id, attached.json()["conversation"]["project_id"])

                    archived_conversation = client.post(f"/api/conversations/{conversation_id}/archive")
                    self.assertEqual(200, archived_conversation.status_code)
                    self.assertEqual([], client.get("/api/conversations").json()["conversations"])
                    self.assertEqual(1, len(client.get("/api/conversations?include_archived=true").json()["conversations"]))

                    created = client.post(
                        "/api/tasks",
                        json={"project_id": project.project_id, "description": "初始任务标题", "thread_id": "thread-1"},
                    )
                    self.assertEqual(200, created.status_code)
                    time.sleep(0.05)
                    renamed_task = client.patch(
                        "/api/tasks/thread-1",
                        json={"display_title": "修复订单参数"},
                    )
                    self.assertEqual(200, renamed_task.status_code)
                    self.assertEqual("修复订单参数", renamed_task.json()["task"]["display_title"])

                    archived_project = client.post(f"/api/projects/{project.project_id}/archive")
                    self.assertEqual(200, archived_project.status_code)
                    self.assertEqual([], client.get("/api/projects").json()["projects"])
                    self.assertEqual(1, len(client.get("/api/projects?include_archived=true").json()["projects"]))
                    blocked = client.post(
                        "/api/conversations",
                        json={"project_id": project.project_id, "display_title": "不能关联"},
                    )
                    self.assertEqual(409, blocked.status_code)
            finally:
                registry.close()
