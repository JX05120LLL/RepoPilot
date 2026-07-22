from __future__ import annotations

import subprocess
import tempfile
import unittest
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path

from repopilot_guard.config import ComponentCheck
from repopilot_guard.context import IndexResult, RetrievalResult, RetrievedContext
from repopilot_guard.graph import (
    ChangePlan,
    CodingGraphFactory,
    EvidenceReference,
    GraphPreflightChecker,
    GraphRunner,
    PhaseOnePreflightResult,
    PlanGenerationResult,
    ResearchDecision,
    SqliteCheckpointStore,
    ToolCall,
)
from repopilot_guard.mcp import McpServerConfig, McpToolDescriptor
from repopilot_guard.mcp_agent import TaskMcpBindingService
from repopilot_guard.mcp_runtime import McpRawToolResult, McpRuntime, McpSessionInfo, McpSessionProtocol, McpToolDiscovery
from repopilot_guard.models import TaskRequest
from repopilot_guard.permissions import PermissionGrant


class ReadyChecker(GraphPreflightChecker):
    def check(self, _repository: Path) -> PhaseOnePreflightResult:
        return PhaseOnePreflightResult(True, (ComponentCheck("all", True, "READY", "测试预检通过。"),))


class ContextService:
    def ingest(self, _workspace: object, _project_id: str, _permission: object) -> IndexResult:
        return IndexResult("READY", "CONTEXT_INDEXED", "测试索引完成。", indexed_chunks=1)

    def retrieve(self, _query: str, _project_id: str, _repo_commit: str) -> RetrievalResult:
        return RetrievalResult(
            "READY",
            "CONTEXT_RETRIEVED",
            "测试检索完成。",
            (RetrievedContext("class OrderService {}", 0.9, "src/main/java/com/example/OrderService.java", 1, 1, "code", "order"),),
        )


class McpCallingModel:
    def __init__(self) -> None:
        self.tool_names: list[str] = []
        self.calls = 0

    def analyze(self, _messages: list[dict[str, str]], tools: tuple[object, ...]) -> ResearchDecision:
        self.tool_names = [str(getattr(tool, "name", "")) for tool in tools]
        self.calls += 1
        return ResearchDecision("查询外部项目文档。", (ToolCall("mcp__docs__search", {"query": "订单权限"}),) if self.calls == 1 else ())

    def plan(self, _messages: list[dict[str, str]], _state: object) -> PlanGenerationResult:
        return PlanGenerationResult(
            ChangePlan(
                summary="根据受控 MCP 文档补充订单权限验证。",
                evidence=[EvidenceReference(source_type="code", path="src/main/java/com/example/OrderService.java", line_start=1, line_end=1, note="服务入口")],
                candidate_files=["src/main/java/com/example/OrderService.java"],
                steps=["根据文档核对租户过滤。"],
                verification=["后续阶段执行 Maven 测试。"],
            )
        )

    def propose_patch(self, _messages: list[dict[str, str]], _state: object) -> object:
        raise AssertionError("计划审批前不得进入补丁阶段")


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def initialize(self) -> McpSessionInfo:
        return McpSessionInfo("图测试 MCP", "1.0", "2025-11-25", False)

    async def list_tools(self, server_name: str) -> McpToolDiscovery:
        return McpToolDiscovery(
            (
                McpToolDescriptor(
                    server_name,
                    "search",
                    "检索研发文档。",
                    {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False},
                ),
            )
        )

    async def call_tool(self, name: str, arguments: dict[str, object]) -> McpRawToolResult:
        self.calls.append((name, arguments))
        return McpRawToolResult(({"type": "text", "text": "订单服务必须校验租户。"},), None, False)

    async def ping(self) -> None:
        return None


class FakeConnector:
    def __init__(self) -> None:
        self.sessions: list[FakeSession] = []

    @asynccontextmanager
    async def open(
        self,
        _config: McpServerConfig,
        _environment: Mapping[str, str],
        _workspace_root: Path | None,
    ) -> AsyncIterator[McpSessionProtocol]:
        session = FakeSession()
        self.sessions.append(session)
        yield session


def create_repository(root: Path) -> Path:
    repository = root / "repository"
    source = repository / "src" / "main" / "java" / "com" / "example"
    source.mkdir(parents=True)
    (source / "OrderService.java").write_text("package com.example; class OrderService {}\n", encoding="utf-8")
    (repository / "pom.xml").write_text("<project><artifactId>demo</artifactId></project>\n", encoding="utf-8")
    config = repository / ".repopilot"
    config.mkdir()
    (config / "mcp.toml").write_text(
        "[[servers]]\nname=\"docs\"\ntransport=\"streamable_http\"\n"
        "url=\"https://mcp.example.com/v1\"\naccess=\"read_only\"\nallowed_tools=[\"search\"]\n",
        encoding="utf-8",
    )
    for args in (("init", "-b", "main"), ("config", "user.name", "RepoPilot Test"), ("config", "user.email", "test@example.invalid")):
        subprocess.run(("git", "-C", str(repository), *args), check=True, capture_output=True)
    subprocess.run(("git", "-C", str(repository), "add", "."), check=True, capture_output=True)
    subprocess.run(("git", "-C", str(repository), "commit", "-m", "fixture"), check=True, capture_output=True)
    return repository


class McpGraphBindingTests(unittest.TestCase):
    def test_explicit_safe_mcp_tool_is_frozen_bound_and_audited_by_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            connector = FakeConnector()
            service = TaskMcpBindingService(
                lambda configuration, workspace_root: McpRuntime(configuration, connector=connector, workspace_root=workspace_root)
            )
            model = McpCallingModel()
            store = SqliteCheckpointStore(root / "state.sqlite")
            graph = CodingGraphFactory(
                ReadyChecker(),
                context_service=ContextService(),
                research_model=model,
                mcp_binding_service=service,
            ).create(store.checkpointer)
            runner = GraphRunner(graph)
            try:
                result = runner.run(
                    TaskRequest(
                        create_repository(root),
                        "查询订单权限文档后生成修复计划",
                        root / "runs",
                        approved_mcp_tools=("mcp__docs__search",),
                    ),
                    "mcp-graph-thread",
                    PermissionGrant.safe(),
                )
            finally:
                store.close()

        self.assertEqual("WAITING_APPROVAL", result.status)
        self.assertIn("mcp__docs__search", model.tool_names)
        self.assertIn("mcp__docs__search", result.state["context_snapshot"]["bound_tool_ids"])
        self.assertIn("mcp__docs__search", result.state["context_snapshot"]["capability_ids"])
        mcp_events = [event for event in result.state["tool_events"] if event.get("type") == "MCP_BINDINGS_DISCOVERED"]
        self.assertEqual("MCP_BINDINGS_READY", mcp_events[0]["code"])
        tool_event = next(event for event in result.state["tool_events"] if event.get("name") == "mcp__docs__search")
        self.assertEqual("MCP_TOOL_COMPLETED", tool_event["code"])
        self.assertEqual(("search", {"query": "订单权限"}), connector.sessions[-1].calls[0])


if __name__ == "__main__":
    unittest.main()
