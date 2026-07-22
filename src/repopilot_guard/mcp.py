"""MCP 服务配置、工具命名空间与连接前信任校验。"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for

from repopilot_guard.capabilities import (
    CapabilityDescriptor,
    CapabilityKind,
    CapabilityRegistry,
    CapabilityRisk,
    CapabilityScope,
)


MAX_MCP_CONFIG_BYTES = 256 * 1024
MAX_MCP_SCHEMA_CHARS = 64 * 1024
_SERVER_NAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ERROR_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")
_SECRET_ARGUMENT_PATTERN = re.compile(r"(?i)(api[-_]?key|token|password|secret|authorization)")
_INLINE_SECRET_KEYS = {
    "api_key",
    "apikey",
    "token",
    "password",
    "secret",
    "authorization",
    "headers",
    "env_values",
    "bearer_token",
}


class McpTransport(str, Enum):
    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable_http"


class McpAccess(str, Enum):
    READ_ONLY = "read_only"
    WRITE = "write"


class McpConfigError(ValueError):
    """配置错误绝不携带原始配置值，避免密钥进入日志。"""

    def __init__(self, code: str, path: Path, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.path = path
        self.message = message

    def to_dict(self) -> dict[str, object]:
        return {"code": self.code, "path": str(self.path), "message": self.message}


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    name: str
    transport: McpTransport
    scope: CapabilityScope
    access: McpAccess
    enabled: bool
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    env: tuple[str, ...] = ()
    bearer_token_env: str | None = None
    allowed_tools: tuple[str, ...] = ()
    startup_timeout_seconds: int = 10
    tool_timeout_seconds: int = 30
    max_result_chars: int = 20_000

    def __post_init__(self) -> None:
        if not _SERVER_NAME_PATTERN.fullmatch(self.name):
            raise ValueError("INVALID_MCP_SERVER_NAME")
        if self.scope is CapabilityScope.BUNDLED:
            raise ValueError("INVALID_MCP_SERVER_SCOPE")
        if self.transport is McpTransport.STDIO:
            if not self.command or self.url is not None or self.bearer_token_env is not None:
                raise ValueError("INVALID_MCP_STDIO_CONFIG")
        elif not self.url or self.command is not None or self.args or self.env:
            raise ValueError("INVALID_MCP_HTTP_CONFIG")
        if len(self.env) != len(set(self.env)) or any(not _ENV_NAME_PATTERN.fullmatch(item) for item in self.env):
            raise ValueError("INVALID_MCP_ENV_REFERENCE")
        if self.bearer_token_env is not None and not _ENV_NAME_PATTERN.fullmatch(self.bearer_token_env):
            raise ValueError("INVALID_MCP_BEARER_ENV_REFERENCE")
        if len(self.allowed_tools) != len(set(self.allowed_tools)) or any(
            not _TOOL_NAME_PATTERN.fullmatch(item) for item in self.allowed_tools
        ):
            raise ValueError("INVALID_MCP_ALLOWED_TOOLS")
        if not 1 <= self.startup_timeout_seconds <= 120:
            raise ValueError("INVALID_MCP_STARTUP_TIMEOUT")
        if not 1 <= self.tool_timeout_seconds <= 600:
            raise ValueError("INVALID_MCP_TOOL_TIMEOUT")
        if not 1_000 <= self.max_result_chars <= 200_000:
            raise ValueError("INVALID_MCP_RESULT_LIMIT")

    @property
    def namespace(self) -> str:
        return f"mcp__{self.name}"

    @property
    def risks(self) -> frozenset[CapabilityRisk]:
        risks = {CapabilityRisk.READ}
        risks.add(CapabilityRisk.PROCESS if self.transport is McpTransport.STDIO else CapabilityRisk.NETWORK)
        if self.access is McpAccess.WRITE:
            risks.add(CapabilityRisk.WRITE)
        return frozenset(risks)

    def capability(self, *, connected: bool = False) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            capability_id=f"mcp_server__{self.name}",
            name=self.name,
            description=f"MCP 服务配置（{self.transport.value}，{self.access.value}）。",
            kind=CapabilityKind.MCP_SERVER,
            scope=self.scope,
            source=f"mcp:{self.name}",
            risks=self.risks,
            enabled=self.enabled,
            metadata={
                "transport": self.transport.value,
                "access": self.access.value,
                "connected": connected,
            },
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "transport": self.transport.value,
            "scope": self.scope.value,
            "access": self.access.value,
            "enabled": self.enabled,
            "command": self.command,
            "args_count": len(self.args),
            "url": self.url,
            "env": list(self.env),
            "bearer_token_env": self.bearer_token_env,
            "allowed_tools": list(self.allowed_tools),
            "startup_timeout_seconds": self.startup_timeout_seconds,
            "tool_timeout_seconds": self.tool_timeout_seconds,
            "max_result_chars": self.max_result_chars,
            "connection_status": "CONFIGURED_NOT_CONNECTED",
        }


@dataclass(frozen=True, slots=True)
class McpConfiguration:
    version: int
    servers: tuple[McpServerConfig, ...]

    def __post_init__(self) -> None:
        if self.version != 1:
            raise ValueError("UNSUPPORTED_MCP_CONFIG_VERSION")
        names = [server.name for server in self.servers]
        if len(names) != len(set(names)):
            raise ValueError("DUPLICATE_MCP_SERVER")

    def to_dict(self) -> dict[str, object]:
        return {
            "status": "READY",
            "version": self.version,
            "servers": [server.to_dict() for server in self.servers],
        }


@dataclass(frozen=True, slots=True)
class McpToolDescriptor:
    server_name: str
    tool_name: str
    description: str
    input_schema: dict[str, object]

    def __post_init__(self) -> None:
        if not _SERVER_NAME_PATTERN.fullmatch(self.server_name):
            raise ValueError("INVALID_MCP_SERVER_NAME")
        if not _TOOL_NAME_PATTERN.fullmatch(self.tool_name):
            raise ValueError("INVALID_MCP_TOOL_NAME")
        if not self.description.strip():
            raise ValueError("MCP_TOOL_DESCRIPTION_REQUIRED")
        if self.input_schema.get("type") != "object":
            raise ValueError("MCP_TOOL_SCHEMA_MUST_BE_OBJECT")
        if len(json.dumps(self.input_schema, ensure_ascii=False)) > MAX_MCP_SCHEMA_CHARS:
            raise ValueError("MCP_TOOL_SCHEMA_TOO_LARGE")
        try:
            validator_for(self.input_schema).check_schema(self.input_schema)
        except SchemaError as error:
            raise ValueError("MCP_TOOL_SCHEMA_INVALID") from error

    @property
    def capability_id(self) -> str:
        return f"mcp__{self.server_name}__{self.tool_name}"


class McpConfigLoader:
    """读取不含密钥值的 TOML 配置；连接与握手由后续 Transport 层负责。"""

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        expected_scope: CapabilityScope = CapabilityScope.PROJECT,
    ) -> McpConfiguration:
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            raise McpConfigError("MCP_CONFIG_NOT_FOUND", resolved, "MCP 配置文件不存在。")
        if resolved.stat().st_size > MAX_MCP_CONFIG_BYTES:
            raise McpConfigError("MCP_CONFIG_TOO_LARGE", resolved, "MCP 配置超过 256 KiB 安全上限。")
        try:
            payload = tomllib.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
            raise McpConfigError("MCP_CONFIG_INVALID", resolved, "MCP TOML 配置无法解析。") from error
        if set(payload) - {"version", "servers"}:
            raise McpConfigError("MCP_CONFIG_UNKNOWN_FIELD", resolved, "MCP 配置包含未知顶层字段。")
        version = payload.get("version", 1)
        server_payloads = payload.get("servers", [])
        if not isinstance(version, int) or not isinstance(server_payloads, list):
            raise McpConfigError("MCP_CONFIG_INVALID", resolved, "MCP version 或 servers 格式无效。")
        servers: list[McpServerConfig] = []
        for item in server_payloads:
            if not isinstance(item, dict):
                raise McpConfigError("MCP_SERVER_NOT_OBJECT", resolved, "每个 MCP server 必须是对象。")
            servers.append(cls._server(item, resolved, expected_scope))
        try:
            return McpConfiguration(version, tuple(servers))
        except ValueError as error:
            raise McpConfigError(str(error), resolved, "MCP 配置未通过一致性校验。") from error

    @staticmethod
    def _server(
        payload: dict[str, object],
        path: Path,
        expected_scope: CapabilityScope,
    ) -> McpServerConfig:
        lowered = {str(key).lower() for key in payload}
        if lowered.intersection(_INLINE_SECRET_KEYS):
            raise McpConfigError("MCP_INLINE_SECRET_BLOCKED", path, "MCP 配置不能保存密钥值，请仅引用环境变量名。")
        allowed = {
            "name",
            "transport",
            "scope",
            "access",
            "enabled",
            "command",
            "args",
            "url",
            "env",
            "bearer_token_env",
            "allowed_tools",
            "startup_timeout_seconds",
            "tool_timeout_seconds",
            "max_result_chars",
        }
        if set(payload) - allowed:
            raise McpConfigError("MCP_SERVER_UNKNOWN_FIELD", path, "MCP server 包含未知字段。")
        try:
            transport = McpTransport(str(payload.get("transport", "")))
            scope = CapabilityScope(str(payload.get("scope", "project")))
            if scope is not expected_scope:
                raise ValueError("MCP_SCOPE_MISMATCH")
            access = McpAccess(str(payload.get("access", "read_only")))
            command = _optional_string(payload.get("command"))
            url = _optional_string(payload.get("url"))
            if url is not None:
                _validate_http_url(url)
            args = _string_tuple(payload.get("args", []), "INVALID_MCP_ARGS")
            if any(_SECRET_ARGUMENT_PATTERN.search(item) for item in args):
                raise ValueError("MCP_SECRET_ARGUMENT_BLOCKED")
            env = _string_tuple(payload.get("env", []), "INVALID_MCP_ENV_REFERENCE")
            allowed_tools = _string_tuple(payload.get("allowed_tools", []), "INVALID_MCP_ALLOWED_TOOLS")
            bearer_token_env = _optional_string(payload.get("bearer_token_env"))
            return McpServerConfig(
                name=str(payload.get("name", "")),
                transport=transport,
                scope=scope,
                access=access,
                enabled=_strict_bool(payload.get("enabled", True)),
                command=command,
                args=args,
                url=url,
                env=env,
                bearer_token_env=bearer_token_env,
                allowed_tools=allowed_tools,
                startup_timeout_seconds=_strict_int(payload.get("startup_timeout_seconds", 10)),
                tool_timeout_seconds=_strict_int(payload.get("tool_timeout_seconds", 30)),
                max_result_chars=_strict_int(payload.get("max_result_chars", 20_000)),
            )
        except (TypeError, ValueError) as error:
            raw_code = str(error)
            code = raw_code if _ERROR_CODE_PATTERN.fullmatch(raw_code) else "MCP_SERVER_INVALID"
            raise McpConfigError(code, path, "MCP server 配置无效。") from error


class McpCapabilityRegistry:
    """保存服务与握手后工具元数据，不负责启动进程或发起网络请求。"""

    def __init__(self, configuration: McpConfiguration) -> None:
        self._servers = {server.name: server for server in configuration.servers}
        self._connected: set[str] = set()
        self._tools: dict[str, tuple[CapabilityDescriptor, ...]] = {}

    @property
    def capabilities(self) -> CapabilityRegistry:
        server_descriptors = (
            server.capability(connected=server.name in self._connected)
            for server in self._servers.values()
        )
        tool_descriptors = (descriptor for descriptors in self._tools.values() for descriptor in descriptors)
        return CapabilityRegistry((*server_descriptors, *tool_descriptors))

    def mark_disconnected(self, server_name: str) -> None:
        if server_name not in self._servers:
            raise ValueError("MCP_SERVER_NOT_REGISTERED")
        self._connected.discard(server_name)

    def tool_capabilities(self, server_name: str) -> tuple[CapabilityDescriptor, ...]:
        return self._tools.get(server_name, ())

    def register_tools(self, server_name: str, tools: Iterable[McpToolDescriptor]) -> tuple[CapabilityDescriptor, ...]:
        server = self._servers.get(server_name)
        if server is None:
            raise ValueError("MCP_SERVER_NOT_REGISTERED")
        if not server.enabled:
            raise ValueError("MCP_SERVER_DISABLED")
        descriptors: list[CapabilityDescriptor] = []
        for tool in tools:
            if tool.server_name != server_name:
                raise ValueError("MCP_TOOL_SERVER_MISMATCH")
            if server.allowed_tools and tool.tool_name not in server.allowed_tools:
                continue
            descriptors.append(
                CapabilityDescriptor(
                    capability_id=tool.capability_id,
                    name=tool.tool_name,
                    description=tool.description,
                    kind=CapabilityKind.MCP_TOOL,
                    scope=server.scope,
                    source=f"mcp:{server_name}",
                    risks=server.risks,
                    metadata={
                        "server": server_name,
                        "input_schema": tool.input_schema,
                        "connected": True,
                    },
                )
            )
        self._tools[server_name] = tuple(descriptors)
        self._connected.add(server_name)
        return tuple(descriptors)


def _validate_http_url(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("INVALID_MCP_HTTP_URL")
    localhost = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and localhost):
        raise ValueError("INVALID_MCP_HTTP_URL")
    if not parsed.hostname:
        raise ValueError("INVALID_MCP_HTTP_URL")


def _string_tuple(value: object, code: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(code)
    return tuple(item.strip() for item in value)


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError("INVALID_MCP_STRING")
    return value.strip()


def _strict_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("INVALID_MCP_BOOLEAN")
    return value


def _strict_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("INVALID_MCP_INTEGER")
    return value
