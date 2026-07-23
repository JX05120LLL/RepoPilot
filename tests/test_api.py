from __future__ import annotations

import json
import tempfile
import time
import unittest
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from repopilot_guard.api import create_app
from repopilot_guard.config import ComponentCheck
from repopilot_guard.context import ManagedDocumentStore
from repopilot_guard.mcp import McpServerConfig, McpToolDescriptor
from repopilot_guard.mcp_runtime import (
    McpRawToolResult,
    McpRuntime,
    McpSessionInfo,
    McpSessionProtocol,
    McpToolDiscovery,
)
from repopilot_guard.plugins import PluginRegistry
from repopilot_guard.project_registry import ProjectRegistry


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

    def resume(self, thread_id: str, approved: bool) -> object:
        self.result.pending_approval = False
        self.result.status = "REPORT"
        self.result.verdict = "BLOCKED" if not approved else "UNVERIFIED"
        return self.result

    def request_cancellation(self, thread_id: str, reason: str | None = None) -> None:
        self.cancellation_requests.append((thread_id, reason))


class CheckpointThenFailingRunner(FakeRunner):
    """模拟 checkpoint 停在 PATCH 后，后台进程又发生未处理异常。"""

    def run(self, request: object, thread_id: str | None, permission: object) -> object:
        self.ran = True
        self.result.status = "PATCH"
        self.result.state["status"] = "PATCH"
        self.result.state["task_operation"] = request.operation.value
        self.result.state["task_description"] = request.description
        raise RuntimeError("不得返回给客户端的内部错误")


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
                self.assertEqual("GIT_REPOSITORY_REQUIRED", payload["task_modes"]["safe_isolated"]["code"])
                self.assertEqual("FULL_LOCAL_RESEARCH_ONLY", payload["task_modes"]["full_local"]["code"])
                self.assertEqual("JAVA_MAVEN_PROFILE_READY", payload["profiles"]["java_maven"]["code"])
                self.assertFalse((root / "runs").exists())
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
            registry = ProjectRegistry(root / "state.sqlite")
            plugin_registry = PluginRegistry(root / "state.sqlite")
            try:
                with TestClient(create_app(FakeRunner(), registry, root / "runs", plugin_registry=plugin_registry)) as client:
                    installed = client.post("/api/plugins", json={"source": str(plugin_root)})
                    self.assertEqual(200, installed.status_code)
                    self.assertTrue(installed.json()["plugin"]["active"])

                    listed = client.get("/api/plugins")
                    self.assertEqual("spring-maintenance", listed.json()["plugins"][0]["plugin_id"])
                    self.assertEqual("VERIFIED", listed.json()["plugins"][0]["integrity_status"])

                    disabled = client.post("/api/plugins/spring-maintenance/enabled", json={"enabled": False})
                    self.assertEqual(200, disabled.status_code)
                    self.assertFalse(disabled.json()["plugin"]["enabled"])

                    skill_file.write_text(skill_file.read_text(encoding="utf-8") + "Modified after review.\n", encoding="utf-8")
                    blocked = client.post("/api/plugins/spring-maintenance/enabled", json={"enabled": True})
                    self.assertEqual(409, blocked.status_code)
                    self.assertEqual("PLUGIN_INTEGRITY_CHECK_FAILED", blocked.json()["detail"]["code"])

                    audit = client.get("/api/plugins/audit?plugin_id=spring-maintenance")
                    self.assertEqual(200, audit.status_code)
                    self.assertEqual("PLUGIN_ENABLE_BLOCKED", audit.json()["events"][0]["action"])
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

    def test_local_api_creates_safe_task_and_streams_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repo"
            repository.mkdir()
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

    def test_api_cancels_background_task_then_archives_it_without_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repo"
            repository.mkdir()
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

                    self.assertEqual("BLOCKED", snapshot["status"])
                    self.assertEqual("BLOCKED", snapshot["verdict"])
                    self.assertEqual("TASK_RUNTIME_FAILED: RuntimeError", snapshot["error_summary"])
                    self.assertEqual("change", snapshot["task_operation"])
                    self.assertEqual("验证运行时失败状态", snapshot["task_description"])
                    self.assertNotIn("不得返回给客户端的内部错误", json.dumps(snapshot, ensure_ascii=False))
            finally:
                registry.close()
