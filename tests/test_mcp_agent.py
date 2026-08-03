from __future__ import annotations

import tempfile
import unittest
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Mapping
from pathlib import Path

from tests.plugin_signing import sign_plugin, trust_test_publisher

from repopilot_guard.mcp import McpServerConfig, McpToolDescriptor
from repopilot_guard.mcp_agent import MAX_MCP_TASK_ARTIFACT_CHARS, TaskMcpBindingService
from repopilot_guard.mcp_runtime import McpRawToolResult, McpRuntime, McpSessionInfo, McpSessionProtocol, McpToolDiscovery
from repopilot_guard.permissions import PermissionGrant
from repopilot_guard.plugins import PluginRegistry


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
        self.closes = 0
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
        try:
            yield session
        finally:
            self.closes += 1


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


def write_plugin(root: Path, plugin_id: str = "docs-plugin") -> Path:
    plugin_root = root / plugin_id
    plugin_root.mkdir(parents=True)
    (plugin_root / "repopilot-plugin.json").write_text(
        "{\n"
        '  "schema_version": 1,\n'
        f'  "id": "{plugin_id}",\n'
        '  "name": "外部文档插件",\n'
        '  "version": "1.0.0",\n'
        '  "description": "提供只读研发文档工具。",\n'
        '  "mcp_config": "mcp.toml"\n'
        "}\n",
        encoding="utf-8",
    )
    (plugin_root / "mcp.toml").write_text(
        "[[servers]]\n"
        'name="docs"\n'
        'transport="streamable_http"\n'
        'url="https://mcp.example.com/plugin"\n'
        'access="read_only"\n'
        'allowed_tools=["search"]\n',
        encoding="utf-8",
    )
    sign_plugin(plugin_root)
    return plugin_root


class TaskMcpBindingServiceTests(unittest.TestCase):
    def _service(self, connector: FakeMcpConnector, plugin_registry: PluginRegistry | None = None) -> TaskMcpBindingService:
        return TaskMcpBindingService(
            lambda configuration, workspace_root: McpRuntime(
                configuration,
                connector=connector,
                workspace_root=workspace_root,
            ),
            plugin_registry=plugin_registry,
        )

    def test_safe_approval_binds_verified_plugin_snapshot_and_blocks_after_disable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plugin_registry = PluginRegistry(root / "state.sqlite")
            trust_test_publisher(plugin_registry)
            try:
                installed = plugin_registry.install(write_plugin(root))
                connector = FakeMcpConnector()
                service = self._service(connector, plugin_registry)
                result = service.discover(
                    root / "workspace",
                    PermissionGrant.safe(),
                    ("mcp__docs__search",),
                    approved_mcp_sources=("plugin:docs-plugin",),
                )

                self.assertEqual("MCP_BINDINGS_READY", result.code)
                binding = result.bindings[0]
                self.assertEqual("plugin:docs-plugin", binding.config_source_id)
                self.assertEqual(installed.package_sha256, binding.plugin_package_sha256)
                allowed = service.invoke_in_workspace(binding, {"query": "订单"}, PermissionGrant.safe(), root / "workspace")
                self.assertEqual("READY", allowed["status"])

                plugin_registry.disable("docs-plugin")
                unavailable = service.discover(
                    root / "workspace",
                    PermissionGrant.safe(),
                    ("mcp__docs__search",),
                    approved_mcp_sources=("plugin:docs-plugin",),
                )
                blocked = service.invoke_in_workspace(binding, {"query": "订单"}, PermissionGrant.safe(), root / "workspace")
                self.assertEqual("MCP_APPROVED_SOURCE_UNAVAILABLE", unavailable.code)
                self.assertEqual("MCP_CONFIG_SOURCE_CHANGED", blocked["code"])
            finally:
                plugin_registry.close()

    def test_same_capability_from_project_and_plugin_is_blocked_without_guessing_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            write_config(root)
            plugin_registry = PluginRegistry(root / "state.sqlite")
            trust_test_publisher(plugin_registry)
            try:
                plugin_registry.install(write_plugin(root))
                result = self._service(FakeMcpConnector(), plugin_registry).discover(
                    root,
                    PermissionGrant.safe(),
                    ("mcp__docs__search",),
                    task_id="plugin-conflict",
                )
            finally:
                plugin_registry.close()

        self.assertEqual("BLOCKED", result.status)
        self.assertEqual("MCP_CAPABILITY_ID_CONFLICT", result.code)

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

    def test_large_output_is_written_as_task_artifact_without_entering_agent_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            write_config(root)
            connector = FakeMcpConnector()
            connector.result_text = "x" * 30_000 + "私密尾部标记"
            service = self._service(connector)
            binding = service.discover(root, PermissionGrant.safe(), ("mcp__docs__search",)).bindings[0]

            payload = service.invoke_in_workspace(
                binding,
                {"query": "订单"},
                PermissionGrant.safe(),
                root,
                artifact_output_root=root / "runs",
                artifact_task_id="task-mcp-artifact",
            )

            artifact = payload["artifact"]
            self.assertEqual("READY", artifact["status"])
            self.assertEqual("mcp_tool_output", artifact["kind"])
            self.assertNotIn("私密尾部标记", str(payload))
            self.assertTrue(payload["agent_output_truncated"])
            artifact_path = root / "runs" / "task-mcp-artifact" / str(artifact["relative_path"])
            self.assertTrue(artifact_path.is_file())
            self.assertIn("私密尾部标记", artifact_path.read_text(encoding="utf-8"))

    def test_output_over_artifact_limit_is_not_written_to_task_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            write_config(root)
            connector = FakeMcpConnector()
            connector.result_text = "x" * (MAX_MCP_TASK_ARTIFACT_CHARS + 1)
            service = self._service(connector)
            binding = service.discover(root, PermissionGrant.safe(), ("mcp__docs__search",)).bindings[0]

            payload = service.invoke_in_workspace(
                binding,
                {"query": "订单"},
                PermissionGrant.safe(),
                root,
                artifact_output_root=root / "runs",
                artifact_task_id="task-mcp-too-large",
            )

            self.assertEqual("OMITTED", payload["artifact"]["status"])
            self.assertEqual("MCP_ARTIFACT_TOO_LARGE", payload["artifact"]["code"])
            self.assertFalse((root / "runs" / "task-mcp-too-large" / "mcp").exists())

    def test_task_scoped_runtime_reuses_one_connection_and_releases_on_terminal_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            write_config(root)
            connector = FakeMcpConnector()
            service = self._service(connector)
            binding = service.discover(
                root,
                PermissionGrant.safe(),
                ("mcp__docs__search",),
                task_id="task-mcp-1",
            ).bindings[0]

            first = service.invoke_in_workspace(
                binding, {"query": "订单"}, PermissionGrant.safe(), root, task_id="task-mcp-1"
            )
            second = service.invoke_in_workspace(
                binding, {"query": "租户"}, PermissionGrant.safe(), root, task_id="task-mcp-1"
            )
            released = service.release("task-mcp-1")

        self.assertEqual("READY", first["status"])
        self.assertEqual("READY", second["status"])
        self.assertEqual(1, connector.opens)
        self.assertEqual(1, connector.closes)
        self.assertEqual(
            [("search", {"query": "订单"}), ("search", {"query": "租户"})],
            connector.sessions[0].calls,
        )
        self.assertTrue(released)

    def test_task_scoped_runtime_never_reuses_another_task_connection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            write_config(root)
            connector = FakeMcpConnector()
            service = self._service(connector)
            first = service.discover(root, PermissionGrant.safe(), ("mcp__docs__search",), task_id="task-a")
            second = service.discover(root, PermissionGrant.safe(), ("mcp__docs__search",), task_id="task-b")
            service.release("task-a")
            service.release("task-b")

        self.assertEqual("MCP_BINDINGS_READY", first.code)
        self.assertEqual("MCP_BINDINGS_READY", second.code)
        self.assertEqual(2, connector.opens)
        self.assertEqual(2, connector.closes)

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
