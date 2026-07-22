from __future__ import annotations

import tempfile
import unittest
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Mapping
from pathlib import Path

from repopilot_guard.mcp import McpServerConfig, McpToolDescriptor
from repopilot_guard.mcp_agent import TaskMcpBindingService
from repopilot_guard.mcp_runtime import McpRawToolResult, McpRuntime, McpSessionInfo, McpSessionProtocol, McpToolDiscovery
from repopilot_guard.permissions import PermissionGrant


class FakeMcpSession:
    def __init__(self, *, schema_version: int = 1, result_text: str = "文档证据") -> None:
        self.schema_version = schema_version
        self.result_text = result_text
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def initialize(self) -> McpSessionInfo:
        return McpSessionInfo("测试文档服务", "1.0", "2025-11-25", True)

    async def list_tools(self, server_name: str) -> McpToolDiscovery:
        properties = {"query": {"type": "string"}}
        if self.schema_version == 2:
            properties["limit"] = {"type": "integer"}
        return McpToolDiscovery(
            (
                McpToolDescriptor(
                    server_name,
                    "search",
                    "检索研发文档。",
                    {"type": "object", "properties": properties, "required": ["query"], "additionalProperties": False},
                ),
                McpToolDescriptor(
                    server_name,
                    "other",
                    "不在配置白名单中的工具。",
                    {"type": "object", "properties": {}, "additionalProperties": False},
                ),
            )
        )

    async def call_tool(self, name: str, arguments: dict[str, object]) -> McpRawToolResult:
        self.calls.append((name, arguments))
        return McpRawToolResult(({"type": "text", "text": self.result_text},), {"found": True}, False)

    async def ping(self) -> None:
        return None


class FakeMcpConnector:
    def __init__(self) -> None:
        self.opens = 0
        self.sessions: list[FakeMcpSession] = []
        self.schema_version = 1
        self.result_text = "文档证据"

    @asynccontextmanager
    async def open(
        self,
        _config: McpServerConfig,
        _environment: Mapping[str, str],
        _workspace_root: Path | None,
    ) -> AsyncIterator[McpSessionProtocol]:
        self.opens += 1
        session = FakeMcpSession(schema_version=self.schema_version, result_text=self.result_text)
        self.sessions.append(session)
        yield session


def write_config(root: Path) -> None:
    directory = root / ".repopilot"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "mcp.toml").write_text(
        "[[servers]]\n"
        'name="docs"\n'
        'transport="streamable_http"\n'
        'url="https://mcp.example.com/v1"\n'
        'access="read_only"\n'
        'allowed_tools=["search"]\n'
        "max_result_chars=200000\n",
        encoding="utf-8",
    )


class TaskMcpBindingServiceTests(unittest.TestCase):
    def _service(self, connector: FakeMcpConnector) -> TaskMcpBindingService:
        return TaskMcpBindingService(
            lambda configuration, workspace_root: McpRuntime(
                configuration,
                connector=connector,
                workspace_root=workspace_root,
            )
        )

    def test_safe_mode_without_explicit_tool_never_opens_connector(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            write_config(root)
            connector = FakeMcpConnector()

            result = self._service(connector).discover(root, PermissionGrant.safe())

        self.assertEqual("MCP_NOT_REQUESTED", result.code)
        self.assertEqual(0, connector.opens)

    def test_explicit_safe_approval_freezes_only_selected_tool_and_invokes_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            write_config(root)
            connector = FakeMcpConnector()
            service = self._service(connector)
            result = service.discover(root, PermissionGrant.safe(), ("mcp__docs__search",))

            self.assertEqual("MCP_BINDINGS_READY", result.code)
            self.assertEqual(["mcp__docs__search"], [item.capability_id for item in result.bindings])
            tools = service.langchain_tools(result.bindings, PermissionGrant.safe(), root)
            payload = tools[0].invoke({"query": "订单权限"})

        self.assertEqual("READY", payload["status"])
        self.assertEqual("MCP_TOOL_COMPLETED", payload["code"])
        self.assertEqual(("search", {"query": "订单权限"}), connector.sessions[-1].calls[0])

    def test_config_change_after_discovery_blocks_call_without_reconnect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            write_config(root)
            connector = FakeMcpConnector()
            service = self._service(connector)
            binding = service.discover(root, PermissionGrant.safe(), ("mcp__docs__search",)).bindings[0]
            opens_after_discovery = connector.opens
            (root / ".repopilot" / "mcp.toml").write_text("version = 1\n", encoding="utf-8")

            changed_config = service.invoke_in_workspace(binding, {"query": "订单"}, PermissionGrant.safe(), root)

        self.assertEqual("MCP_CONFIG_CHANGED_AFTER_DISCOVERY", changed_config["code"])
        self.assertEqual(opens_after_discovery, connector.opens)

    def test_schema_change_after_discovery_blocks_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            write_config(root)
            connector = FakeMcpConnector()
            service = self._service(connector)
            binding = service.discover(root, PermissionGrant.safe(), ("mcp__docs__search",)).bindings[0]
            connector.schema_version = 2

            changed_schema = service.invoke_in_workspace(binding, {"query": "订单"}, PermissionGrant.safe(), root)

        self.assertEqual("MCP_TOOL_CHANGED_AFTER_DISCOVERY", changed_schema["code"])

    def test_agent_applies_its_own_output_cap_after_runtime_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            write_config(root)
            connector = FakeMcpConnector()
            connector.result_text = "x" * 30_000
            service = self._service(connector)
            binding = service.discover(root, PermissionGrant.safe(), ("mcp__docs__search",)).bindings[0]

            payload = service.invoke_in_workspace(binding, {"query": "订单"}, PermissionGrant.safe(), root)

        self.assertTrue(payload["truncated"])
        self.assertTrue(payload["agent_output_truncated"])
        self.assertLessEqual(len(str(payload["data"]["preview"])), 20_000)

    def test_missing_explicit_tool_is_blocked_instead_of_binding_another_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            write_config(root)
            result = self._service(FakeMcpConnector()).discover(root, PermissionGrant.safe(), ("mcp__docs__missing",))

        self.assertEqual("BLOCKED", result.status)
        self.assertEqual("MCP_APPROVED_TOOL_NOT_DISCOVERED", result.code)
        self.assertEqual((), result.bindings)


if __name__ == "__main__":
    unittest.main()
