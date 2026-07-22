"""真实 MCP 连接、工具发现和受控调用运行时。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

import httpx
from jsonschema.exceptions import SchemaError, ValidationError as JsonSchemaValidationError
from jsonschema.validators import validator_for
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from repopilot_guard.capabilities import CapabilityDecision, CapabilityDescriptor, CapabilityPolicy
from repopilot_guard.mcp import (
    McpCapabilityRegistry,
    McpConfiguration,
    McpServerConfig,
    McpToolDescriptor,
    McpTransport,
)
from repopilot_guard.permissions import PermissionGrant


MAX_MCP_INPUT_CHARS = 64 * 1024
MAX_MCP_TOOL_PAGES = 20
MAX_MCP_DISCOVERED_TOOLS = 256
MAX_MCP_RUNTIME_EVENTS = 10_000
DEFAULT_CONNECT_ATTEMPTS = 2
DEFAULT_RETRY_BACKOFF_SECONDS = 0.5
DEFAULT_CIRCUIT_FAILURES = 3
DEFAULT_CIRCUIT_COOLDOWN_SECONDS = 60


class McpConnectionState(str, Enum):
    CONFIGURED = "CONFIGURED"
    DISABLED = "DISABLED"
    INITIALIZING = "INITIALIZING"
    READY = "READY"
    NEEDS_AUTH = "NEEDS_AUTH"
    UNAVAILABLE = "UNAVAILABLE"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"


class McpRuntimeError(RuntimeError):
    """只携带稳定错误码，不向上游传播 SDK/网络原始异常文本。"""

    def __init__(self, code: str, state: McpConnectionState = McpConnectionState.UNAVAILABLE) -> None:
        super().__init__(code)
        self.code = code
        self.state = state


@dataclass(frozen=True, slots=True)
class McpSessionInfo:
    server_name: str
    server_version: str
    protocol_version: str
    instructions_present: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "server_name": self.server_name,
            "server_version": self.server_version,
            "protocol_version": self.protocol_version,
            "instructions_present": self.instructions_present,
            "instructions_security_label": "UNTRUSTED_MCP_SERVER_INSTRUCTIONS",
        }


@dataclass(frozen=True, slots=True)
class McpToolIssue:
    tool_name: str
    code: str

    def to_dict(self) -> dict[str, str]:
        return {"tool_name": self.tool_name, "code": self.code}


@dataclass(frozen=True, slots=True)
class McpToolDiscovery:
    tools: tuple[McpToolDescriptor, ...]
    issues: tuple[McpToolIssue, ...] = ()


@dataclass(frozen=True, slots=True)
class McpRawToolResult:
    content: tuple[dict[str, object], ...]
    structured_content: dict[str, object] | None
    is_error: bool


class McpSessionProtocol(Protocol):
    async def initialize(self) -> McpSessionInfo: ...

    async def list_tools(self, server_name: str) -> McpToolDiscovery: ...

    async def call_tool(self, name: str, arguments: dict[str, object]) -> McpRawToolResult: ...

    async def ping(self) -> None: ...


class McpConnectorProtocol(Protocol):
    def open(
        self,
        config: McpServerConfig,
        environment: Mapping[str, str],
        workspace_root: Path | None,
    ) -> AbstractAsyncContextManager[McpSessionProtocol]: ...


@dataclass(frozen=True, slots=True)
class McpServerSnapshot:
    server_name: str
    state: McpConnectionState
    code: str
    message: str
    changed_at: str
    tool_count: int = 0
    rejected_tool_count: int = 0
    failure_count: int = 0
    session_info: McpSessionInfo | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "server_name": self.server_name,
            "state": self.state.value,
            "code": self.code,
            "message": self.message,
            "changed_at": self.changed_at,
            "tool_count": self.tool_count,
            "rejected_tool_count": self.rejected_tool_count,
            "failure_count": self.failure_count,
            "session_info": self.session_info.to_dict() if self.session_info else None,
        }


@dataclass(frozen=True, slots=True)
class McpRuntimeEvent:
    sequence: int
    event_type: str
    server_name: str
    code: str
    occurred_at: str
    tool_name: str | None = None
    duration_ms: int | None = None
    argument_keys: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "event_type": self.event_type,
            "server_name": self.server_name,
            "code": self.code,
            "occurred_at": self.occurred_at,
            "tool_name": self.tool_name,
            "duration_ms": self.duration_ms,
            "argument_keys": list(self.argument_keys),
        }


@dataclass(frozen=True, slots=True)
class McpConnectResult:
    status: str
    code: str
    snapshot: McpServerSnapshot
    tools: tuple[CapabilityDescriptor, ...] = ()
    decision: CapabilityDecision | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "code": self.code,
            "server": self.snapshot.to_dict(),
            "tools": [tool.to_dict() for tool in self.tools],
            "decision": self.decision.to_dict() if self.decision else None,
        }


@dataclass(frozen=True, slots=True)
class McpToolCallResult:
    status: str
    code: str
    server_name: str
    tool_name: str
    data: dict[str, object]
    truncated: bool
    output_sha256: str
    original_chars: int
    duration_ms: int

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "code": self.code,
            "server_name": self.server_name,
            "tool_name": self.tool_name,
            "data": self.data,
            "truncated": self.truncated,
            "output_sha256": self.output_sha256,
            "original_chars": self.original_chars,
            "duration_ms": self.duration_ms,
            "security_label": "UNTRUSTED_MCP_TOOL_OUTPUT",
        }


class OfficialMcpSession:
    """将 MCP SDK v1 的对象转换为 RepoPilot 稳定领域类型。"""

    def __init__(self, session: ClientSession) -> None:
        self._session = session

    async def initialize(self) -> McpSessionInfo:
        result = await self._session.initialize()
        server_info = result.serverInfo
        return McpSessionInfo(
            server_name=str(server_info.name),
            server_version=str(server_info.version or "unknown"),
            protocol_version=str(result.protocolVersion),
            instructions_present=bool(result.instructions),
        )

    async def list_tools(self, server_name: str) -> McpToolDiscovery:
        tools: list[McpToolDescriptor] = []
        issues: list[McpToolIssue] = []
        cursor: str | None = None
        for _ in range(MAX_MCP_TOOL_PAGES):
            page = await self._session.list_tools(cursor=cursor)
            for tool in page.tools:
                if len(tools) >= MAX_MCP_DISCOVERED_TOOLS:
                    issues.append(McpToolIssue("<catalog>", "MCP_TOOL_LIMIT_REACHED"))
                    return McpToolDiscovery(tuple(tools), tuple(issues))
                try:
                    tools.append(
                        McpToolDescriptor(
                            server_name=server_name,
                            tool_name=str(tool.name),
                            description=(tool.description or f"MCP 工具 {tool.name}。").strip(),
                            input_schema=dict(tool.inputSchema),
                        )
                    )
                except (TypeError, ValueError) as error:
                    code = str(error) if _is_error_code(str(error)) else "MCP_TOOL_DESCRIPTOR_INVALID"
                    issues.append(McpToolIssue(str(getattr(tool, "name", "<unknown>")), code))
            cursor = page.nextCursor
            if not cursor:
                return McpToolDiscovery(tuple(tools), tuple(issues))
        issues.append(McpToolIssue("<catalog>", "MCP_TOOL_PAGE_LIMIT_REACHED"))
        return McpToolDiscovery(tuple(tools), tuple(issues))

    async def call_tool(self, name: str, arguments: dict[str, object]) -> McpRawToolResult:
        result = await self._session.call_tool(name, arguments=arguments)
        content = tuple(_sanitize_content_block(block) for block in result.content)
        structured = _json_object(result.structuredContent) if result.structuredContent is not None else None
        return McpRawToolResult(content, structured, bool(result.isError))

    async def ping(self) -> None:
        await self._session.send_ping()


class OfficialMcpConnector:
    """MCP SDK v1 Transport Adapter；SDK 类型不会泄漏到其他模块。"""

    @asynccontextmanager
    async def open(
        self,
        config: McpServerConfig,
        environment: Mapping[str, str],
        workspace_root: Path | None,
    ) -> AsyncIterator[McpSessionProtocol]:
        async with AsyncExitStack() as stack:
            if config.transport is McpTransport.STDIO:
                if workspace_root is None:
                    raise McpRuntimeError("MCP_WORKSPACE_ROOT_REQUIRED")
                root = workspace_root.expanduser().resolve()
                if not root.is_dir():
                    raise McpRuntimeError("MCP_WORKSPACE_ROOT_INVALID")
                errlog = stack.enter_context(open(os.devnull, "w", encoding="utf-8"))
                params = StdioServerParameters(
                    command=_resolve_stdio_command(config.command),
                    args=list(config.args),
                    env=dict(environment),
                    cwd=root,
                )
                read_stream, write_stream = await stack.enter_async_context(stdio_client(params, errlog=errlog))
            else:
                headers: dict[str, str] = {}
                if config.bearer_token_env:
                    headers["Authorization"] = f"Bearer {environment[config.bearer_token_env]}"
                client = await stack.enter_async_context(
                    httpx.AsyncClient(
                        headers=headers,
                        timeout=httpx.Timeout(config.tool_timeout_seconds),
                        follow_redirects=False,
                        trust_env=False,
                    )
                )
                read_stream, write_stream, _ = await stack.enter_async_context(
                    streamable_http_client(str(config.url), http_client=client, terminate_on_close=True)
                )
            session = await stack.enter_async_context(
                ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(seconds=config.tool_timeout_seconds),
                )
            )
            yield OfficialMcpSession(session)


def _resolve_stdio_command(command: str | None) -> str:
    """让示例中的裸 Python 命令稳定使用启动 RepoPilot 的解释器。

    STDIO MCP 子进程继承系统 PATH 时，Windows 很容易拾取与 RepoPilot 依赖
    不一致的 Python。仅对无目录的通用 Python 命令做映射；用户显式配置的
    相对路径、绝对路径或其它运行时命令保持不变。
    """

    if command is None:
        raise McpRuntimeError("MCP_STDIO_COMMAND_MISSING")
    if command.casefold() in {"python", "python3", "python.exe", "python3.exe"}:
        return str(Path(sys.executable).resolve())
    return command


@dataclass(slots=True)
class _ActorCommand:
    operation: str
    future: asyncio.Future[object]
    tool_name: str | None = None
    arguments: dict[str, object] | None = None


class _McpServerActor:
    """保证 SDK context manager 的进入、使用和退出发生在同一个异步任务。"""

    def __init__(
        self,
        config: McpServerConfig,
        connector: McpConnectorProtocol,
        environment: Mapping[str, str],
        workspace_root: Path | None,
        failure_count: int,
    ) -> None:
        self.config = config
        self._connector = connector
        self._environment = dict(environment)
        self._workspace_root = workspace_root
        self._queue: asyncio.Queue[_ActorCommand] = asyncio.Queue()
        self._ready: asyncio.Future[McpServerSnapshot] | None = None
        self._task: asyncio.Task[None] | None = None
        self.discovery = McpToolDiscovery(())
        self.snapshot = _snapshot(
            config.name,
            McpConnectionState.CONFIGURED,
            "MCP_CONFIGURED",
            "MCP 服务已配置，尚未连接。",
            failure_count=failure_count,
        )

    async def start(self) -> McpServerSnapshot:
        if self._task is not None:
            return self.snapshot
        loop = asyncio.get_running_loop()
        self._ready = loop.create_future()
        self._task = asyncio.create_task(self._run(), name=f"repopilot-mcp-{self.config.name}")
        return await asyncio.shield(self._ready)

    async def ping(self) -> None:
        await self._request("ping")

    async def call_tool(self, name: str, arguments: dict[str, object]) -> McpRawToolResult:
        result = await self._request("call", name, arguments)
        if not isinstance(result, McpRawToolResult):
            raise McpRuntimeError("MCP_INVALID_TOOL_RESULT")
        return result

    async def stop(self) -> McpServerSnapshot:
        if self._task is None or self._task.done():
            return self.snapshot
        with suppress(McpRuntimeError):
            await self._request("close")
        with suppress(asyncio.CancelledError, Exception):
            await self._task
        return self.snapshot

    async def _request(
        self,
        operation: str,
        tool_name: str | None = None,
        arguments: dict[str, object] | None = None,
    ) -> object:
        if self._task is None or self._task.done() or self.snapshot.state is not McpConnectionState.READY:
            raise McpRuntimeError("MCP_SERVER_NOT_READY", self.snapshot.state)
        future: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        await self._queue.put(_ActorCommand(operation, future, tool_name, arguments))
        return await future

    async def _run(self) -> None:
        assert self._ready is not None
        self.snapshot = _snapshot(
            self.config.name,
            McpConnectionState.INITIALIZING,
            "MCP_INITIALIZING",
            "正在初始化 MCP 连接。",
            failure_count=self.snapshot.failure_count,
        )
        stack = AsyncExitStack()
        try:
            async with asyncio.timeout(self.config.startup_timeout_seconds):
                session = await stack.enter_async_context(
                    self._connector.open(self.config, self._environment, self._workspace_root)
                )
                session_info = await session.initialize()
                self.discovery = await session.list_tools(self.config.name)
            self.snapshot = _snapshot(
                self.config.name,
                McpConnectionState.READY,
                "MCP_READY",
                "MCP 连接和工具发现完成。",
                tool_count=len(self.discovery.tools),
                rejected_tool_count=len(self.discovery.issues),
                failure_count=self.snapshot.failure_count,
                session_info=session_info,
            )
            self._ready.set_result(self.snapshot)
            while True:
                command = await self._queue.get()
                if command.operation == "close":
                    self.snapshot = _snapshot(
                        self.config.name,
                        McpConnectionState.CLOSING,
                        "MCP_CLOSING",
                        "正在关闭 MCP 连接。",
                        tool_count=len(self.discovery.tools),
                        rejected_tool_count=len(self.discovery.issues),
                        failure_count=self.snapshot.failure_count,
                        session_info=self.snapshot.session_info,
                    )
                    _set_future_result(command.future, None)
                    break
                try:
                    async with asyncio.timeout(self.config.tool_timeout_seconds):
                        if command.operation == "ping":
                            await session.ping()
                            _set_future_result(command.future, None)
                        elif command.operation == "call" and command.tool_name and command.arguments is not None:
                            result = await session.call_tool(command.tool_name, command.arguments)
                            _set_future_result(command.future, result)
                        else:
                            _set_future_error(command.future, McpRuntimeError("MCP_ACTOR_COMMAND_INVALID"))
                except TimeoutError:
                    _set_future_error(command.future, McpRuntimeError("MCP_TOOL_TIMEOUT"))
                    raise McpRuntimeError("MCP_TOOL_TIMEOUT")
                except Exception as error:
                    runtime_error = error if isinstance(error, McpRuntimeError) else McpRuntimeError("MCP_TOOL_CALL_UNAVAILABLE")
                    _set_future_error(command.future, runtime_error)
                    raise runtime_error from error
        except asyncio.CancelledError:
            if not self._ready.done():
                self._ready.set_result(
                    _snapshot(self.config.name, McpConnectionState.CLOSED, "MCP_CANCELLED", "MCP 连接已取消。")
                )
            raise
        except BaseException as error:
            state, code = _classify_connection_error(error)
            self.snapshot = _snapshot(
                self.config.name,
                state,
                code,
                "MCP 连接不可用。" if state is McpConnectionState.UNAVAILABLE else "MCP 服务需要认证。",
                failure_count=self.snapshot.failure_count + 1,
            )
            if not self._ready.done():
                self._ready.set_result(self.snapshot)
        finally:
            with suppress(BaseException):
                await stack.aclose()
            if self.snapshot.state in {McpConnectionState.CLOSING, McpConnectionState.READY}:
                self.snapshot = _snapshot(
                    self.config.name,
                    McpConnectionState.CLOSED,
                    "MCP_CLOSED",
                    "MCP 连接已关闭。",
                    tool_count=len(self.discovery.tools),
                    rejected_tool_count=len(self.discovery.issues),
                    failure_count=self.snapshot.failure_count,
                    session_info=self.snapshot.session_info,
                )
            self._fail_pending()

    def _fail_pending(self) -> None:
        while not self._queue.empty():
            command = self._queue.get_nowait()
            _set_future_error(command.future, McpRuntimeError("MCP_SERVER_NOT_READY", self.snapshot.state))


class McpRuntime:
    """面向 CLI/API/Agent 的 MCP Host，统一连接、策略、Schema 和审计。"""

    def __init__(
        self,
        configuration: McpConfiguration,
        *,
        connector: McpConnectorProtocol | None = None,
        workspace_root: Path | None = None,
        connect_attempts: int = DEFAULT_CONNECT_ATTEMPTS,
        retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
        circuit_failures: int = DEFAULT_CIRCUIT_FAILURES,
        circuit_cooldown_seconds: int = DEFAULT_CIRCUIT_COOLDOWN_SECONDS,
        event_sink: Callable[[McpRuntimeEvent], None] | None = None,
    ) -> None:
        if connect_attempts < 1 or retry_backoff_seconds < 0 or circuit_failures < 1 or circuit_cooldown_seconds < 1:
            raise ValueError("INVALID_MCP_RUNTIME_LIMITS")
        self._configuration = configuration
        self._configs = {server.name: server for server in configuration.servers}
        self._registry = McpCapabilityRegistry(configuration)
        self._connector = connector or OfficialMcpConnector()
        self._workspace_root = workspace_root.expanduser().resolve() if workspace_root else None
        self._connect_attempts = connect_attempts
        self._retry_backoff_seconds = retry_backoff_seconds
        self._circuit_failures = circuit_failures
        self._circuit_cooldown_seconds = circuit_cooldown_seconds
        self._event_sink = event_sink
        self._events: list[McpRuntimeEvent] = []
        self._sequence = 0
        self._actors: dict[str, _McpServerActor] = {}
        self._locks: dict[str, asyncio.Lock] = {name: asyncio.Lock() for name in self._configs}
        self._failures: dict[str, int] = {name: 0 for name in self._configs}
        self._circuit_until: dict[str, float] = {}
        self._snapshots = {
            server.name: _snapshot(
                server.name,
                McpConnectionState.CONFIGURED if server.enabled else McpConnectionState.DISABLED,
                "MCP_CONFIGURED" if server.enabled else "MCP_DISABLED",
                "MCP 服务已配置，尚未连接。" if server.enabled else "MCP 服务已禁用。",
            )
            for server in configuration.servers
        }

    @property
    def capabilities(self) -> McpCapabilityRegistry:
        return self._registry

    @property
    def events(self) -> tuple[McpRuntimeEvent, ...]:
        return tuple(self._events)

    def status(self, server_name: str | None = None) -> tuple[McpServerSnapshot, ...]:
        self._refresh_actor_snapshots()
        if server_name is not None:
            snapshot = self._snapshots.get(server_name)
            if snapshot is None:
                raise ValueError("MCP_SERVER_NOT_REGISTERED")
            return (snapshot,)
        return tuple(self._snapshots[name] for name in sorted(self._snapshots))

    async def connect(
        self,
        server_name: str,
        permission: PermissionGrant,
        *,
        approved: bool = False,
        force: bool = False,
    ) -> McpConnectResult:
        self._config(server_name)
        async with self._locks[server_name]:
            return await self._connect(server_name, permission, approved=approved, force=force)

    async def _connect(
        self,
        server_name: str,
        permission: PermissionGrant,
        *,
        approved: bool,
        force: bool,
    ) -> McpConnectResult:
        config = self._config(server_name)
        decision = CapabilityPolicy().decide(config.capability(), permission, approved=approved)
        if not decision.allowed:
            snapshot = self._snapshots[server_name]
            self._event("MCP_CONNECT_BLOCKED", server_name, decision.code)
            return McpConnectResult("BLOCKED", decision.code, snapshot, decision=decision)
        existing = self._actors.get(server_name)
        if existing and existing.snapshot.state is McpConnectionState.READY:
            tools = self._registry.tool_capabilities(server_name)
            return McpConnectResult("READY", "MCP_ALREADY_READY", existing.snapshot, tools, decision)
        if not config.enabled:
            return McpConnectResult("BLOCKED", "MCP_DISABLED", self._snapshots[server_name], decision=decision)
        missing = self._missing_environment(config)
        if missing:
            snapshot = _snapshot(
                server_name,
                McpConnectionState.NEEDS_AUTH,
                "MCP_ENV_MISSING",
                "MCP 服务所需环境变量未配置。",
                failure_count=self._failures[server_name],
            )
            self._snapshots[server_name] = snapshot
            self._event("MCP_CONNECT_BLOCKED", server_name, "MCP_ENV_MISSING")
            return McpConnectResult("BLOCKED", "MCP_ENV_MISSING", snapshot, decision=decision)
        if not force and time.monotonic() < self._circuit_until.get(server_name, 0):
            snapshot = _snapshot(
                server_name,
                McpConnectionState.UNAVAILABLE,
                "MCP_CIRCUIT_OPEN",
                "MCP 连接连续失败，熔断器暂时开启。",
                failure_count=self._failures[server_name],
            )
            self._snapshots[server_name] = snapshot
            return McpConnectResult("BLOCKED", "MCP_CIRCUIT_OPEN", snapshot, decision=decision)
        if force:
            self._circuit_until.pop(server_name, None)

        environment = self._environment(config)
        last_snapshot = self._snapshots[server_name]
        for attempt in range(self._connect_attempts):
            started = time.monotonic()
            actor = _McpServerActor(
                config,
                self._connector,
                environment,
                self._workspace_root,
                self._failures[server_name],
            )
            self._actors[server_name] = actor
            last_snapshot = await actor.start()
            self._snapshots[server_name] = last_snapshot
            duration_ms = _elapsed_ms(started)
            if last_snapshot.state is McpConnectionState.READY:
                tools = self._registry.register_tools(server_name, actor.discovery.tools)
                self._failures[server_name] = 0
                self._circuit_until.pop(server_name, None)
                self._event("MCP_CONNECTED", server_name, "MCP_READY", duration_ms=duration_ms)
                return McpConnectResult("READY", "MCP_READY", last_snapshot, tools, decision)
            self._actors.pop(server_name, None)
            self._failures[server_name] += 1
            self._event("MCP_CONNECT_FAILED", server_name, last_snapshot.code, duration_ms=duration_ms)
            if last_snapshot.state is McpConnectionState.NEEDS_AUTH:
                break
            if attempt + 1 < self._connect_attempts:
                await asyncio.sleep(self._retry_backoff_seconds * (attempt + 1))
        if last_snapshot.state is McpConnectionState.UNAVAILABLE and self._failures[server_name] >= self._circuit_failures:
            self._circuit_until[server_name] = time.monotonic() + self._circuit_cooldown_seconds
        return McpConnectResult("BLOCKED", last_snapshot.code, last_snapshot, decision=decision)

    async def ping(self, server_name: str) -> dict[str, object]:
        try:
            actor = self._ready_actor(server_name)
        except McpRuntimeError as error:
            return {"status": "BLOCKED", "code": error.code, "server_name": server_name}
        started = time.monotonic()
        try:
            await actor.ping()
        except McpRuntimeError as error:
            await actor.stop()
            self._capture_actor_failure(server_name, actor, error.code)
            return {"status": "BLOCKED", "code": error.code, "server_name": server_name}
        duration_ms = _elapsed_ms(started)
        self._event("MCP_PING", server_name, "MCP_PING_OK", duration_ms=duration_ms)
        return {"status": "READY", "code": "MCP_PING_OK", "server_name": server_name, "duration_ms": duration_ms}

    async def call_tool(
        self,
        capability_id: str,
        arguments: dict[str, object],
        permission: PermissionGrant,
        *,
        approved: bool = False,
    ) -> McpToolCallResult:
        if not isinstance(arguments, dict):
            return _blocked_call("MCP_INVALID_TOOL_ARGUMENTS", "", capability_id)
        try:
            input_json = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return _blocked_call("MCP_INVALID_TOOL_ARGUMENTS", "", capability_id)
        if len(input_json) > MAX_MCP_INPUT_CHARS:
            return _blocked_call("MCP_TOOL_INPUT_TOO_LARGE", "", capability_id)

        descriptor = self._registry.capabilities.get(capability_id)
        if descriptor is None or descriptor.kind.value != "mcp_tool":
            return _blocked_call("MCP_TOOL_NOT_DISCOVERED", "", capability_id)
        server_name = str(descriptor.metadata.get("server", ""))
        try:
            actor = self._ready_actor(server_name)
        except McpRuntimeError as error:
            return _blocked_call(error.code, server_name, capability_id)
        decision = CapabilityPolicy().decide(descriptor, permission, approved=approved)
        if not decision.allowed:
            self._event("MCP_TOOL_BLOCKED", server_name, decision.code, tool_name=capability_id, argument_keys=tuple(sorted(arguments)))
            return _blocked_call(decision.code, server_name, capability_id)
        schema = descriptor.metadata.get("input_schema")
        if not isinstance(schema, Mapping):
            return _blocked_call("MCP_TOOL_SCHEMA_MISSING", server_name, capability_id)
        try:
            validator_type = validator_for(schema)
            validator_type.check_schema(schema)
            validator_type(schema).validate(arguments)
        except (SchemaError, JsonSchemaValidationError):
            self._event(
                "MCP_TOOL_BLOCKED",
                server_name,
                "MCP_TOOL_ARGUMENT_SCHEMA_INVALID",
                tool_name=capability_id,
                argument_keys=tuple(sorted(arguments)),
            )
            return _blocked_call("MCP_TOOL_ARGUMENT_SCHEMA_INVALID", server_name, capability_id)

        started = time.monotonic()
        try:
            raw = await actor.call_tool(descriptor.name, arguments)
        except McpRuntimeError as error:
            duration_ms = _elapsed_ms(started)
            await actor.stop()
            self._capture_actor_failure(server_name, actor, error.code)
            self._event(
                "MCP_TOOL_FAILED",
                server_name,
                error.code,
                tool_name=capability_id,
                duration_ms=duration_ms,
                argument_keys=tuple(sorted(arguments)),
            )
            return _blocked_call(error.code, server_name, capability_id, duration_ms)
        duration_ms = _elapsed_ms(started)
        result = _bounded_tool_result(self._configs[server_name], capability_id, raw, duration_ms)
        self._event(
            "MCP_TOOL_COMPLETED",
            server_name,
            result.code,
            tool_name=capability_id,
            duration_ms=duration_ms,
            argument_keys=tuple(sorted(arguments)),
        )
        return result

    async def disconnect(self, server_name: str) -> McpServerSnapshot:
        self._config(server_name)
        async with self._locks[server_name]:
            return await self._disconnect(server_name)

    async def _disconnect(self, server_name: str) -> McpServerSnapshot:
        actor = self._actors.pop(server_name, None)
        if actor is not None:
            await actor.stop()
            snapshot = actor.snapshot
        else:
            snapshot = _snapshot(server_name, McpConnectionState.CLOSED, "MCP_CLOSED", "MCP 连接已关闭。")
        self._registry.mark_disconnected(server_name)
        self._snapshots[server_name] = snapshot
        self._event("MCP_DISCONNECTED", server_name, snapshot.code)
        return snapshot

    async def close(self) -> None:
        await asyncio.gather(*(self.disconnect(server_name) for server_name in tuple(self._actors)))

    async def __aenter__(self) -> "McpRuntime":
        return self

    async def __aexit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        await self.close()

    def _config(self, server_name: str) -> McpServerConfig:
        config = self._configs.get(server_name)
        if config is None:
            raise ValueError("MCP_SERVER_NOT_REGISTERED")
        return config

    def _ready_actor(self, server_name: str) -> _McpServerActor:
        actor = self._actors.get(server_name)
        if actor is None or actor.snapshot.state is not McpConnectionState.READY:
            raise McpRuntimeError("MCP_SERVER_NOT_READY")
        return actor

    @staticmethod
    def _missing_environment(config: McpServerConfig) -> tuple[str, ...]:
        names = set(config.env)
        if config.bearer_token_env:
            names.add(config.bearer_token_env)
        return tuple(sorted(name for name in names if not os.getenv(name)))

    @staticmethod
    def _environment(config: McpServerConfig) -> dict[str, str]:
        names = set(config.env)
        if config.bearer_token_env:
            names.add(config.bearer_token_env)
        return {name: os.environ[name] for name in names}

    def _capture_actor_failure(self, server_name: str, actor: _McpServerActor, code: str) -> None:
        self._snapshots[server_name] = actor.snapshot
        self._failures[server_name] += 1
        if self._failures[server_name] >= self._circuit_failures:
            self._circuit_until[server_name] = time.monotonic() + self._circuit_cooldown_seconds
        self._registry.mark_disconnected(server_name)
        self._actors.pop(server_name, None)
        self._event("MCP_CONNECTION_LOST", server_name, code)

    def _refresh_actor_snapshots(self) -> None:
        for name, actor in tuple(self._actors.items()):
            self._snapshots[name] = actor.snapshot
            if actor.snapshot.state is not McpConnectionState.READY and actor._task and actor._task.done():
                self._actors.pop(name, None)

    def _event(
        self,
        event_type: str,
        server_name: str,
        code: str,
        *,
        tool_name: str | None = None,
        duration_ms: int | None = None,
        argument_keys: tuple[str, ...] = (),
    ) -> None:
        self._sequence += 1
        event = McpRuntimeEvent(
            self._sequence,
            event_type,
            server_name,
            code,
            _now(),
            tool_name,
            duration_ms,
            argument_keys,
        )
        self._events.append(event)
        if len(self._events) > MAX_MCP_RUNTIME_EVENTS:
            del self._events[: len(self._events) - MAX_MCP_RUNTIME_EVENTS]
        if self._event_sink:
            with suppress(Exception):
                self._event_sink(event)


def _snapshot(
    server_name: str,
    state: McpConnectionState,
    code: str,
    message: str,
    *,
    tool_count: int = 0,
    rejected_tool_count: int = 0,
    failure_count: int = 0,
    session_info: McpSessionInfo | None = None,
) -> McpServerSnapshot:
    return McpServerSnapshot(
        server_name,
        state,
        code,
        message,
        _now(),
        tool_count,
        rejected_tool_count,
        failure_count,
        session_info,
    )


def _classify_connection_error(error: BaseException) -> tuple[McpConnectionState, str]:
    if isinstance(error, McpRuntimeError):
        return error.state, error.code
    if isinstance(error, TimeoutError):
        return McpConnectionState.UNAVAILABLE, "MCP_STARTUP_TIMEOUT"
    if isinstance(error, BaseExceptionGroup):
        for nested in error.exceptions:
            state, code = _classify_connection_error(nested)
            if state is McpConnectionState.NEEDS_AUTH:
                return state, code
        return McpConnectionState.UNAVAILABLE, "MCP_CONNECTION_FAILED"
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code in {401, 403}:
        return McpConnectionState.NEEDS_AUTH, "MCP_AUTH_REQUIRED"
    lowered = str(error).lower()
    if any(marker in lowered for marker in ("401", "403", "unauthorized", "forbidden")):
        return McpConnectionState.NEEDS_AUTH, "MCP_AUTH_REQUIRED"
    return McpConnectionState.UNAVAILABLE, "MCP_CONNECTION_FAILED"


def _sanitize_content_block(block: object) -> dict[str, object]:
    if hasattr(block, "model_dump"):
        payload = block.model_dump(mode="json", by_alias=True, exclude_none=True)
    else:
        payload = {"type": type(block).__name__}
    if not isinstance(payload, dict):
        return {"type": type(block).__name__}
    _remove_binary_data(payload)
    return _json_object(payload)


def _remove_binary_data(payload: dict[str, object]) -> None:
    data = payload.get("data")
    if isinstance(data, str) and payload.get("type") in {"image", "audio"}:
        payload["data_sha256"] = hashlib.sha256(data.encode("utf-8")).hexdigest()
        payload["data_chars"] = len(data)
        payload["data_omitted"] = True
        payload.pop("data", None)
    resource = payload.get("resource")
    if isinstance(resource, dict):
        blob = resource.get("blob")
        if isinstance(blob, str):
            resource["blob_sha256"] = hashlib.sha256(blob.encode("utf-8")).hexdigest()
            resource["blob_chars"] = len(blob)
            resource["blob_omitted"] = True
            resource.pop("blob", None)


def _json_object(value: object) -> dict[str, object]:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        return {"value": decoded}
    return decoded


def _bounded_tool_result(
    config: McpServerConfig,
    capability_id: str,
    raw: McpRawToolResult,
    duration_ms: int,
) -> McpToolCallResult:
    payload: dict[str, object] = {
        "content": list(raw.content),
        "structured_content": raw.structured_content,
        "is_error": raw.is_error,
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    truncated = len(serialized) > config.max_result_chars
    data = (
        {
            "preview": serialized[: config.max_result_chars],
            "content_omitted": True,
        }
        if truncated
        else payload
    )
    return McpToolCallResult(
        "FAILED" if raw.is_error else "READY",
        "MCP_TOOL_REPORTED_ERROR" if raw.is_error else "MCP_TOOL_COMPLETED",
        config.name,
        capability_id,
        data,
        truncated,
        digest,
        len(serialized),
        duration_ms,
    )


def _blocked_call(
    code: str,
    server_name: str,
    tool_name: str,
    duration_ms: int = 0,
) -> McpToolCallResult:
    digest = hashlib.sha256(b"{}").hexdigest()
    return McpToolCallResult("BLOCKED", code, server_name, tool_name, {}, False, digest, 0, duration_ms)


def _set_future_result(future: asyncio.Future[object], value: object) -> None:
    if not future.done():
        future.set_result(value)


def _set_future_error(future: asyncio.Future[object], error: McpRuntimeError) -> None:
    if not future.done():
        future.set_exception(error)


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_error_code(value: str) -> bool:
    return bool(value) and len(value) <= 80 and all(character.isupper() or character.isdigit() or character == "_" for character in value)
