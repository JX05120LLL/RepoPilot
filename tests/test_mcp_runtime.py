from __future__ import annotations

import asyncio
import io
import json
import socket
import subprocess
import sys
import time
import unittest
from contextlib import asynccontextmanager
from contextlib import redirect_stdout
from pathlib import Path
from typing import AsyncIterator, Mapping

from repopilot_guard.capabilities import CapabilityScope
from repopilot_guard.cli import main as cli_main
from repopilot_guard.mcp import McpAccess, McpConfiguration, McpServerConfig, McpToolDescriptor, McpTransport
from repopilot_guard.mcp_runtime import (
    McpConnectionState,
    McpRawToolResult,
    McpRuntime,
    McpSessionInfo,
    McpSessionProtocol,
    McpToolDiscovery,
    _resolve_stdio_command,
)
from repopilot_guard.permissions import FULL_ACCESS_CONFIRMATION, PermissionGrant, PermissionMode


def _remote_config(
    *,
    bearer_token_env: str | None = None,
    max_result_chars: int = 2_000,
    tool_timeout_seconds: int = 2,
) -> McpServerConfig:
    return McpServerConfig(
        name="docs",
        transport=McpTransport.STREAMABLE_HTTP,
        scope=CapabilityScope.PROJECT,
        access=McpAccess.READ_ONLY,
        enabled=True,
        url="https://mcp.example.com/v1",
        bearer_token_env=bearer_token_env,
        allowed_tools=("search",),
        startup_timeout_seconds=2,
        tool_timeout_seconds=tool_timeout_seconds,
        max_result_chars=max_result_chars,
    )


def _full() -> PermissionGrant:
    return PermissionGrant(PermissionMode.FULL, FULL_ACCESS_CONFIRMATION)


class FakeSession:
    def __init__(
        self,
        *,
        result: McpRawToolResult | None = None,
        call_delay: float = 0,
        initialize_delay: float = 0,
    ) -> None:
        self.result = result or McpRawToolResult(({"type": "text", "text": "ok"},), {"ok": True}, False)
        self.call_delay = call_delay
        self.initialize_delay = initialize_delay
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.pings = 0

    async def initialize(self) -> McpSessionInfo:
        if self.initialize_delay:
            await asyncio.sleep(self.initialize_delay)
        return McpSessionInfo("Fake Docs", "1.0", "2025-11-25", True)

    async def list_tools(self, server_name: str) -> McpToolDiscovery:
        return McpToolDiscovery(
            (
                McpToolDescriptor(
                    server_name,
                    "search",
                    "搜索工程文档。",
                    {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                ),
            )
        )

    async def call_tool(self, name: str, arguments: dict[str, object]) -> McpRawToolResult:
        self.calls.append((name, arguments))
        if self.call_delay:
            await asyncio.sleep(self.call_delay)
        return self.result

    async def ping(self) -> None:
        self.pings += 1


class FakeConnector:
    def __init__(self, session: FakeSession | None = None, *, failures: int = 0) -> None:
        self.session = session or FakeSession()
        self.failures = failures
        self.opens = 0
        self.closes = 0

    @asynccontextmanager
    async def open(
        self,
        _config: McpServerConfig,
        _environment: Mapping[str, str],
        _workspace_root: Path | None,
    ) -> AsyncIterator[McpSessionProtocol]:
        self.opens += 1
        if self.opens <= self.failures:
            raise OSError("connection unavailable")
        try:
            yield self.session
        finally:
            self.closes += 1


class McpRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def test_bare_python_command_uses_repopilot_runtime_without_rewriting_explicit_path(self) -> None:
        self.assertEqual(str(Path(sys.executable).resolve()), _resolve_stdio_command("python"))
        self.assertEqual(str(Path(sys.executable).resolve()), _resolve_stdio_command("python3.exe"))
        self.assertEqual("tools/python.exe", _resolve_stdio_command("tools/python.exe"))

    async def test_safe_remote_connection_requires_approval_before_connector_opens(self) -> None:
        connector = FakeConnector()
        runtime = McpRuntime(McpConfiguration(1, (_remote_config(),)), connector=connector)

        blocked = await runtime.connect("docs", PermissionGrant.safe())
        self.assertEqual(0, connector.opens)
        ready = await runtime.connect("docs", PermissionGrant.safe(), approved=True)

        self.assertEqual("CAPABILITY_APPROVAL_REQUIRED", blocked.code)
        self.assertEqual(1, connector.opens)
        self.assertEqual("READY", ready.status)
        self.assertEqual("MCP_READY", ready.code)
        self.assertEqual(["mcp__docs__search"], [tool.capability_id for tool in ready.tools])
        self.assertEqual(McpConnectionState.READY, runtime.status("docs")[0].state)
        await runtime.close()
        self.assertEqual(1, connector.closes)

    async def test_concurrent_connect_is_coalesced_to_one_session(self) -> None:
        connector = FakeConnector(FakeSession(initialize_delay=0.05))
        runtime = McpRuntime(McpConfiguration(1, (_remote_config(),)), connector=connector)

        first, second = await asyncio.gather(
            runtime.connect("docs", PermissionGrant.safe(), approved=True),
            runtime.connect("docs", PermissionGrant.safe(), approved=True),
        )

        self.assertEqual(1, connector.opens)
        self.assertEqual({"MCP_READY", "MCP_ALREADY_READY"}, {first.code, second.code})
        await runtime.close()

    async def test_schema_is_checked_before_tool_call_and_audit_excludes_values(self) -> None:
        session = FakeSession()
        runtime = McpRuntime(McpConfiguration(1, (_remote_config(),)), connector=FakeConnector(session))
        await runtime.connect("docs", PermissionGrant.safe(), approved=True)

        invalid = await runtime.call_tool(
            "mcp__docs__search",
            {"query": 42},
            PermissionGrant.safe(),
            approved=True,
        )
        valid = await runtime.call_tool(
            "mcp__docs__search",
            {"query": "private-value"},
            PermissionGrant.safe(),
            approved=True,
        )

        self.assertEqual("MCP_TOOL_ARGUMENT_SCHEMA_INVALID", invalid.code)
        self.assertEqual(1, len(session.calls))
        self.assertEqual("READY", valid.status)
        self.assertNotIn("private-value", str([event.to_dict() for event in runtime.events]))
        self.assertIn("query", str([event.to_dict() for event in runtime.events]))
        await runtime.close()

    async def test_tool_output_is_bounded_and_hashed(self) -> None:
        result = McpRawToolResult(({"type": "text", "text": "x" * 3_000},), None, False)
        runtime = McpRuntime(
            McpConfiguration(1, (_remote_config(max_result_chars=1_000),)),
            connector=FakeConnector(FakeSession(result=result)),
        )
        await runtime.connect("docs", PermissionGrant.safe(), approved=True)

        called = await runtime.call_tool(
            "mcp__docs__search",
            {"query": "java"},
            PermissionGrant.safe(),
            approved=True,
        )

        self.assertTrue(called.truncated)
        self.assertGreater(called.original_chars, 1_000)
        self.assertEqual(64, len(called.output_sha256))
        self.assertLessEqual(len(str(called.data["preview"])), 1_000)
        await runtime.close()

    async def test_server_reported_error_is_failed_not_success(self) -> None:
        result = McpRawToolResult(({"type": "text", "text": "upstream failed"},), None, True)
        runtime = McpRuntime(
            McpConfiguration(1, (_remote_config(),)),
            connector=FakeConnector(FakeSession(result=result)),
        )
        await runtime.connect("docs", PermissionGrant.safe(), approved=True)

        called = await runtime.call_tool(
            "mcp__docs__search",
            {"query": "java"},
            PermissionGrant.safe(),
            approved=True,
        )

        self.assertEqual("FAILED", called.status)
        self.assertEqual("MCP_TOOL_REPORTED_ERROR", called.code)
        await runtime.close()

    async def test_tool_timeout_closes_connection_and_returns_blocked(self) -> None:
        runtime = McpRuntime(
            McpConfiguration(1, (_remote_config(tool_timeout_seconds=1),)),
            connector=FakeConnector(FakeSession(call_delay=1.2)),
            connect_attempts=1,
        )
        await runtime.connect("docs", PermissionGrant.safe(), approved=True)

        called = await runtime.call_tool(
            "mcp__docs__search",
            {"query": "java"},
            PermissionGrant.safe(),
            approved=True,
        )

        self.assertEqual("BLOCKED", called.status)
        self.assertEqual("MCP_TOOL_TIMEOUT", called.code)
        self.assertNotEqual(McpConnectionState.READY, runtime.status("docs")[0].state)
        await runtime.close()

    async def test_repeated_connection_failure_opens_circuit(self) -> None:
        connector = FakeConnector(failures=10)
        runtime = McpRuntime(
            McpConfiguration(1, (_remote_config(),)),
            connector=connector,
            connect_attempts=1,
            retry_backoff_seconds=0,
            circuit_failures=2,
        )

        first = await runtime.connect("docs", PermissionGrant.safe(), approved=True)
        second = await runtime.connect("docs", PermissionGrant.safe(), approved=True)
        third = await runtime.connect("docs", PermissionGrant.safe(), approved=True)

        self.assertEqual("MCP_CONNECTION_FAILED", first.code)
        self.assertEqual("MCP_CONNECTION_FAILED", second.code)
        self.assertEqual("MCP_CIRCUIT_OPEN", third.code)
        self.assertEqual(2, connector.opens)

    async def test_missing_bearer_environment_never_opens_connector(self) -> None:
        connector = FakeConnector()
        runtime = McpRuntime(
            McpConfiguration(1, (_remote_config(bearer_token_env="REPOPILOT_TEST_MCP_TOKEN_MISSING"),)),
            connector=connector,
        )

        result = await runtime.connect("docs", PermissionGrant.safe(), approved=True)

        self.assertEqual("MCP_ENV_MISSING", result.code)
        self.assertEqual(McpConnectionState.NEEDS_AUTH, result.snapshot.state)
        self.assertEqual(0, connector.opens)

    async def test_event_sink_failure_does_not_change_runtime_result(self) -> None:
        def broken_sink(_event: object) -> None:
            raise RuntimeError("audit backend unavailable")

        runtime = McpRuntime(
            McpConfiguration(1, (_remote_config(),)),
            connector=FakeConnector(),
            event_sink=broken_sink,
        )

        result = await runtime.connect("docs", PermissionGrant.safe(), approved=True)

        self.assertEqual("READY", result.status)
        await runtime.close()


class McpOfficialSdkIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_server_initialize_discover_call_and_close(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        server_script = repository / "tests" / "fixtures" / "mcp_echo_server.py"
        config = McpServerConfig(
            name="local-echo",
            transport=McpTransport.STDIO,
            scope=CapabilityScope.PROJECT,
            access=McpAccess.READ_ONLY,
            enabled=True,
            command=sys.executable,
            args=(str(server_script),),
            allowed_tools=("echo",),
            startup_timeout_seconds=10,
            tool_timeout_seconds=5,
            max_result_chars=4_000,
        )
        runtime = McpRuntime(
            McpConfiguration(1, (config,)),
            workspace_root=repository,
            connect_attempts=1,
        )

        connected = await runtime.connect("local-echo", _full())
        called = await runtime.call_tool(
            "mcp__local-echo__echo",
            {"message": "你好，MCP"},
            _full(),
        )
        pinged = await runtime.ping("local-echo")
        closed = await runtime.disconnect("local-echo")

        self.assertEqual("READY", connected.status)
        self.assertEqual("READY", called.status)
        self.assertIn("你好，MCP", str(called.data))
        self.assertEqual("READY", pinged["status"])
        self.assertEqual(McpConnectionState.CLOSED, closed.state)

    async def test_real_streamable_http_server_initialize_discover_call_and_close(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        server_script = repository / "tests" / "fixtures" / "mcp_http_server.py"
        with socket.socket() as candidate:
            candidate.bind(("127.0.0.1", 0))
            port = int(candidate.getsockname()[1])
        process = subprocess.Popen(
            [sys.executable, str(server_script), "--port", str(port)],
            cwd=repository,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            await asyncio.to_thread(self._wait_for_port, port, process)
            config = McpServerConfig(
                name="local-http",
                transport=McpTransport.STREAMABLE_HTTP,
                scope=CapabilityScope.PROJECT,
                access=McpAccess.READ_ONLY,
                enabled=True,
                url=f"http://127.0.0.1:{port}/mcp",
                allowed_tools=("multiply",),
                startup_timeout_seconds=10,
                tool_timeout_seconds=5,
                max_result_chars=4_000,
            )
            runtime = McpRuntime(McpConfiguration(1, (config,)), connect_attempts=1)

            connected = await runtime.connect("local-http", PermissionGrant.safe(), approved=True)
            called = await runtime.call_tool(
                "mcp__local-http__multiply",
                {"left": 6, "right": 7},
                PermissionGrant.safe(),
                approved=True,
            )
            closed = await runtime.disconnect("local-http")

            self.assertEqual("READY", connected.status)
            self.assertEqual("READY", called.status)
            self.assertEqual(42, called.data["structured_content"]["result"])
            self.assertEqual(McpConnectionState.CLOSED, closed.state)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    @staticmethod
    def _wait_for_port(port: int, process: subprocess.Popen[bytes]) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("MCP_HTTP_TEST_SERVER_EXITED")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("MCP_HTTP_TEST_SERVER_TIMEOUT")


class McpCliTests(unittest.TestCase):
    def test_call_accepts_arguments_file_on_windows(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = cli_main(
                [
                    "mcp",
                    "call",
                    "--config",
                    str(repository / "examples" / "mcp.local-echo.toml"),
                    "--server",
                    "local-echo",
                    "--workspace-root",
                    str(repository),
                    "--permission",
                    "full",
                    "--confirm-full-access",
                    FULL_ACCESS_CONFIRMATION,
                    "--tool",
                    "mcp__local-echo__echo",
                    "--arguments-file",
                    str(repository / "examples" / "mcp.echo.arguments.json"),
                ]
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(0, exit_code)
        self.assertEqual("READY", payload["status"])
        self.assertEqual("MCP_TOOL_COMPLETED", payload["operation"]["code"])
        self.assertEqual("CLOSED", payload["closed"]["state"])


if __name__ == "__main__":
    unittest.main()
