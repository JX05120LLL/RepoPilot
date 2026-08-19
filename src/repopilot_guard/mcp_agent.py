"""将经任务授权的只读 MCP 工具安全接入研究图。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from threading import Event, RLock, Thread
from typing import TYPE_CHECKING, Any, TypeVar

from langchain_core.tools import StructuredTool
from pydantic import ConfigDict, create_model

from repopilot_guard.capabilities import (
    CapabilityDescriptor,
    CapabilityKind,
    CapabilityRegistry,
    CapabilityRisk,
    CapabilityScope,
)
from repopilot_guard.mcp import McpAccess, McpConfigError, McpConfigLoader, McpConfiguration
from repopilot_guard.mcp_runtime import McpRuntime, McpToolCallResult
from repopilot_guard.permissions import PermissionGrant

if TYPE_CHECKING:
    from repopilot_guard.plugins import PluginRegistry


MCP_CONFIG_RELATIVE_PATH = Path(".repopilot") / "mcp.toml"
MAX_AGENT_MCP_RESULT_CHARS = 20_000
MAX_MCP_TASK_ARTIFACT_CHARS = 512 * 1024
_SAFE_TASK_ID = re.compile(r"^[^/\\\\]{1,128}$")
_CONFIG_SOURCE_ID = re.compile(r"^(?:project|plugin:[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?)$")
_T = TypeVar("_T")
McpRuntimeFactory = Callable[[McpConfiguration, Path], McpRuntime]


@dataclass(frozen=True, slots=True)
class McpConfigurationSource:
    """已通过控制面筛选的 MCP 配置来源。

    `config_path` 只在本机运行时使用；任务状态仅保存 source_id、配置哈希和
    可选插件包哈希，避免泄漏项目或插件快照路径。
    """

    source_id: str
    config_path: Path
    plugin_package_sha256: str | None = None

    def __post_init__(self) -> None:
        if not _CONFIG_SOURCE_ID.fullmatch(self.source_id):
            raise ValueError("MCP_CONFIG_SOURCE_INVALID")
        if self.source_id == "project" and self.plugin_package_sha256 is not None:
            raise ValueError("MCP_CONFIG_SOURCE_INVALID")
        if self.source_id.startswith("plugin:") and (
            self.plugin_package_sha256 is None or not re.fullmatch(r"[0-9a-f]{64}", self.plugin_package_sha256)
        ):
            raise ValueError("MCP_CONFIG_SOURCE_INVALID")


class _TaskMcpRuntimeSession:
    """让同一任务的 MCP Runtime 固定在单独事件循环，避免跨 loop 复用 Actor。"""

    def __init__(
        self,
        runtime_factory: McpRuntimeFactory,
        configuration: McpConfiguration,
        workspace_root: Path,
        source_id: str,
        config_sha256: str,
        full_access: bool,
    ) -> None:
        self._runtime_factory = runtime_factory
        self._configuration = configuration
        self.workspace_root = workspace_root.expanduser().resolve()
        self.source_id = source_id
        self.config_sha256 = config_sha256
        self.full_access = full_access
        self._lock = RLock()
        self._ready = Event()
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._runtime: McpRuntime | None = None
        self._startup_error: BaseException | None = None
        self._thread = Thread(target=self._run, name="repopilot-task-mcp", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("MCP_TASK_RUNTIME_START_TIMEOUT")
        if self._startup_error is not None:
            raise RuntimeError("MCP_TASK_RUNTIME_START_FAILED") from self._startup_error

    def matches(self, workspace_root: Path, source_id: str, config_sha256: str, full_access: bool) -> bool:
        return (
            self.workspace_root == workspace_root.expanduser().resolve()
            and self.source_id == source_id
            and self.config_sha256 == config_sha256
            and self.full_access == full_access
            and not self._closed
        )

    def invoke(self, operation: Callable[[McpRuntime], Awaitable[_T]]) -> _T:
        with self._lock:
            if self._closed or self._loop is None or self._runtime is None:
                raise RuntimeError("MCP_TASK_RUNTIME_CLOSED")
            future = asyncio.run_coroutine_threadsafe(operation(self._runtime), self._loop)
            return future.result()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop = self._loop
            runtime = self._runtime
            if loop is not None and runtime is not None:
                try:
                    asyncio.run_coroutine_threadsafe(runtime.close(), loop).result(timeout=30)
                except Exception:
                    # 关闭失败也必须停止专用 loop，不能让任务资源在后台无限存活。
                    pass
                loop.call_soon_threadsafe(loop.stop)
        self._thread.join(timeout=5)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            self._runtime = self._runtime_factory(self._configuration, self.workspace_root)
        except BaseException as error:
            self._startup_error = error
        finally:
            self._ready.set()
        if self._startup_error is None:
            loop.run_forever()
        runtime = self._runtime
        if runtime is not None:
            try:
                loop.run_until_complete(runtime.close())
            except Exception:
                pass
        loop.close()


@dataclass(frozen=True, slots=True)
class McpToolBinding:
    """任务开始时冻结的单个 MCP 工具元数据，不包含连接或密钥。"""

    capability_id: str
    server_name: str
    tool_name: str
    description: str
    input_schema: dict[str, object]
    schema_sha256: str
    config_sha256: str
    risks: tuple[str, ...]
    config_source_id: str = "project"
    plugin_package_sha256: str | None = None

    @classmethod
    def from_descriptor(
        cls,
        descriptor: CapabilityDescriptor,
        config_sha256: str,
        source: McpConfigurationSource,
    ) -> "McpToolBinding":
        schema = descriptor.metadata.get("input_schema")
        server_name = descriptor.metadata.get("server")
        if not isinstance(schema, dict) or not isinstance(server_name, str):
            raise ValueError("MCP_TOOL_BINDING_METADATA_INVALID")
        return cls(
            capability_id=descriptor.capability_id,
            server_name=server_name,
            tool_name=descriptor.name,
            description=descriptor.description,
            input_schema=dict(schema),
            schema_sha256=_sha256_json(schema),
            config_sha256=config_sha256,
            config_source_id=source.source_id,
            plugin_package_sha256=source.plugin_package_sha256,
            risks=tuple(sorted(risk.value for risk in descriptor.risks)),
        )

    @classmethod
    def from_dict(cls, payload: object) -> "McpToolBinding":
        if not isinstance(payload, dict):
            raise ValueError("MCP_TOOL_BINDING_INVALID")
        try:
            schema = payload["input_schema"]
            if not isinstance(schema, dict):
                raise ValueError("MCP_TOOL_BINDING_INVALID")
            binding = cls(
                capability_id=str(payload["capability_id"]),
                server_name=str(payload["server_name"]),
                tool_name=str(payload["tool_name"]),
                description=str(payload["description"]),
                input_schema=dict(schema),
                schema_sha256=str(payload["schema_sha256"]),
                config_sha256=str(payload["config_sha256"]),
                config_source_id=str(payload.get("config_source_id", "project")),
                plugin_package_sha256=(
                    str(payload["plugin_package_sha256"])
                    if payload.get("plugin_package_sha256") is not None
                    else None
                ),
                risks=tuple(str(item) for item in payload["risks"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("MCP_TOOL_BINDING_INVALID") from error
        if (
            not binding.capability_id.startswith("mcp__")
            or not binding.server_name
            or not binding.tool_name
            or len(binding.schema_sha256) != 64
            or len(binding.config_sha256) != 64
            or not _CONFIG_SOURCE_ID.fullmatch(binding.config_source_id)
            or (
                binding.config_source_id == "project"
                and binding.plugin_package_sha256 is not None
            )
            or (
                binding.config_source_id.startswith("plugin:")
                and (
                    binding.plugin_package_sha256 is None
                    or not re.fullmatch(r"[0-9a-f]{64}", binding.plugin_package_sha256)
                )
            )
            or not binding.risks
            or any(item not in {risk.value for risk in CapabilityRisk} for item in binding.risks)
            or binding.schema_sha256 != _sha256_json(binding.input_schema)
            or not _valid_object_schema(binding.input_schema)
        ):
            raise ValueError("MCP_TOOL_BINDING_INVALID")
        return binding

    def descriptor(self) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            capability_id=self.capability_id,
            name=self.tool_name,
            description=self.description,
            kind=CapabilityKind.MCP_TOOL,
            scope=CapabilityScope.PROJECT,
            source=f"mcp:{self.config_source_id}:{self.server_name}",
            risks=frozenset(CapabilityRisk(item) for item in self.risks),
            metadata={
                "server": self.server_name,
                "input_schema": self.input_schema,
                "frozen": True,
                "config_source_id": self.config_source_id,
            },
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "capability_id": self.capability_id,
            "server_name": self.server_name,
            "tool_name": self.tool_name,
            "description": self.description,
            "input_schema": self.input_schema,
            "schema_sha256": self.schema_sha256,
            "config_sha256": self.config_sha256,
            "config_source_id": self.config_source_id,
            "plugin_package_sha256": self.plugin_package_sha256,
            "risks": list(self.risks),
        }


@dataclass(frozen=True, slots=True)
class McpBindingResult:
    status: str
    code: str
    message: str
    bindings: tuple[McpToolBinding, ...] = ()
    issues: tuple[dict[str, str], ...] = ()

    def to_event(self) -> dict[str, object]:
        return {
            "type": "MCP_BINDINGS_DISCOVERED",
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "bindings": [binding.to_dict() for binding in self.bindings],
            "issues": [dict(issue) for issue in self.issues],
        }


class TaskMcpBindingService:
    """在图外执行 MCP 连接，图内只接收经过冻结的工具清单。"""

    def __init__(
        self,
        runtime_factory: McpRuntimeFactory | None = None,
        plugin_registry: "PluginRegistry | None" = None,
    ) -> None:
        self._runtime_factory = runtime_factory or (lambda configuration, root: McpRuntime(configuration, workspace_root=root))
        self._plugin_registry = plugin_registry
        self._lock = RLock()
        self._task_sessions: dict[tuple[str, str], _TaskMcpRuntimeSession] = {}

    def discover(
        self,
        workspace_root: Path,
        permission: PermissionGrant,
        approved_mcp_tools: Iterable[str] = (),
        *,
        approved_mcp_sources: Iterable[str] = (),
        task_id: str | None = None,
    ) -> McpBindingResult:
        approved = frozenset(approved_mcp_tools)
        approved_sources = frozenset(approved_mcp_sources)
        if any(not _CONFIG_SOURCE_ID.fullmatch(source_id) for source_id in approved_sources):
            return McpBindingResult("BLOCKED", "MCP_APPROVED_SOURCE_INVALID", "任务 MCP 配置来源无效，未建立外部连接。")
        if approved_sources and not approved:
            return McpBindingResult("BLOCKED", "MCP_APPROVED_SOURCE_WITHOUT_TOOL", "MCP 来源审批必须同时指定允许的工具。")
        if not permission.is_full_access and not approved:
            return McpBindingResult("READY", "MCP_NOT_REQUESTED", "安全模式未显式批准 MCP 工具，未建立外部连接。")
        sources = self._configuration_sources(workspace_root)
        if approved_sources:
            available_sources = {source.source_id for source in sources}
            missing_sources = sorted(approved_sources - available_sources)
            if missing_sources:
                return McpBindingResult(
                    "BLOCKED",
                    "MCP_APPROVED_SOURCE_UNAVAILABLE",
                    "已批准的 MCP 配置来源不可用、已禁用或未通过完整性校验。",
                )
            sources = tuple(source for source in sources if source.source_id in approved_sources)
        if not sources:
            if approved:
                return McpBindingResult("BLOCKED", "MCP_CONFIG_NOT_FOUND", "已批准 MCP 工具，但项目或已启用插件未提供可用 MCP 配置。")
            return McpBindingResult("READY", "MCP_NOT_CONFIGURED", "项目和已启用插件未配置 MCP，继续使用内置研究工具。")

        bindings: list[McpToolBinding] = []
        issues: list[dict[str, str]] = []
        discovered_ids: set[str] = set()
        binding_owners: dict[str, str] = {}
        for source in sources:
            try:
                configuration = McpConfigLoader.load(source.config_path)
                config_sha256 = _sha256_bytes(source.config_path.read_bytes())
            except McpConfigError as error:
                issues.append({"source_id": source.source_id, "code": error.code})
                continue
            except OSError:
                issues.append({"source_id": source.source_id, "code": "MCP_CONFIG_READ_FAILED"})
                continue
            for server in sorted(configuration.servers, key=lambda item: item.name):
                requested_for_server = tuple(item for item in approved if item.startswith(f"mcp__{server.name}__"))
                if not permission.is_full_access and not requested_for_server:
                    continue
                if server.access is not McpAccess.READ_ONLY:
                    issues.append({"source_id": source.source_id, "server_name": server.name, "code": "MCP_WRITE_SERVER_NOT_RESEARCH_BINDABLE"})
                    continue
                try:
                    result = self._connect_server(
                        configuration,
                        workspace_root,
                        source.source_id,
                        config_sha256,
                        server.name,
                        permission,
                        task_id=task_id,
                    )
                except Exception:
                    issues.append({"source_id": source.source_id, "server_name": server.name, "code": "MCP_DISCOVERY_UNAVAILABLE"})
                    continue
                if result.status != "READY":
                    issues.append({"source_id": source.source_id, "server_name": server.name, "code": result.code})
                    continue
                for descriptor in result.tools:
                    if not permission.is_full_access and descriptor.capability_id not in approved:
                        continue
                    existing_source = binding_owners.get(descriptor.capability_id)
                    if existing_source is not None and existing_source != source.source_id:
                        self.release(task_id)
                        return McpBindingResult(
                            "BLOCKED",
                            "MCP_CAPABILITY_ID_CONFLICT",
                            "项目与插件 MCP 配置声明了相同工具 ID，任务不会猜测调用目标。",
                            issues=tuple(issues),
                        )
                    try:
                        binding = McpToolBinding.from_descriptor(descriptor, config_sha256, source)
                    except ValueError:
                        issues.append({"source_id": source.source_id, "server_name": server.name, "code": "MCP_TOOL_BINDING_METADATA_INVALID"})
                        continue
                    bindings.append(binding)
                    binding_owners[binding.capability_id] = source.source_id
                    discovered_ids.add(binding.capability_id)

        missing = sorted(approved - discovered_ids)
        if missing:
            self.release(task_id)
            return McpBindingResult(
                "BLOCKED",
                "MCP_APPROVED_TOOL_NOT_DISCOVERED",
                "已批准的 MCP 工具未被当前配置和服务发现，任务不会扩大到其他工具。",
                tuple(sorted(bindings, key=lambda item: item.capability_id)),
                tuple(issues),
            )
        if not bindings:
            self.release(task_id)
            code = "MCP_BINDING_UNAVAILABLE" if issues else "MCP_NO_READ_ONLY_TOOLS"
            return McpBindingResult("READY", code, "没有可绑定的只读 MCP 工具，继续使用内置研究工具。", issues=tuple(issues))
        return McpBindingResult(
            "READY",
            "MCP_BINDINGS_READY",
            "已冻结本任务允许使用的 MCP 工具快照。",
            tuple(sorted(bindings, key=lambda item: item.capability_id)),
            tuple(issues),
        )

    def langchain_tools(
        self,
        bindings: Iterable[McpToolBinding],
        permission: PermissionGrant,
        workspace_root: Path,
        *,
        task_id: str | None = None,
        artifact_output_root: Path | None = None,
        artifact_task_id: str | None = None,
    ) -> tuple[StructuredTool, ...]:
        root = workspace_root.expanduser().resolve()
        return tuple(
            self._langchain_tool(
                binding,
                permission,
                root,
                task_id=task_id,
                artifact_output_root=artifact_output_root,
                artifact_task_id=artifact_task_id,
            )
            for binding in bindings
        )

    def invoke_in_workspace(
        self,
        binding: McpToolBinding,
        arguments: dict[str, object],
        permission: PermissionGrant,
        workspace_root: Path,
        *,
        task_id: str | None = None,
        artifact_output_root: Path | None = None,
        artifact_task_id: str | None = None,
    ) -> dict[str, object]:
        source = self._source_for_binding(binding, workspace_root)
        if source is None:
            return _blocked("MCP_CONFIG_SOURCE_CHANGED", "MCP 配置来源在任务开始后不可用、被禁用或版本发生变化，已阻断调用。")
        try:
            if _sha256_bytes(source.config_path.read_bytes()) != binding.config_sha256:
                return _blocked("MCP_CONFIG_CHANGED_AFTER_DISCOVERY", "MCP 配置在任务开始后发生变化，已阻断调用。")
            configuration = McpConfigLoader.load(source.config_path)
        except McpConfigError as error:
            return _blocked(error.code, "MCP 配置无效，已阻断调用。")
        except OSError:
            return _blocked("MCP_CONFIG_READ_FAILED", "MCP 配置无法读取，已阻断调用。")
        try:
            result = self._call_server(
                configuration,
                workspace_root,
                binding,
                arguments,
                permission,
                source.source_id,
                task_id=task_id,
            )
        except Exception:
            return _blocked("MCP_TOOL_CALL_UNAVAILABLE", "MCP 工具调用不可用，未返回伪造结果。")
        payload = result.to_dict()
        artifact = _persist_large_mcp_output(
            result,
            output_root=artifact_output_root,
            task_id=artifact_task_id,
        )
        if artifact is not None:
            payload["artifact"] = artifact
        return _bound_agent_result(payload)

    def release(self, task_id: str | None) -> bool:
        """仅释放指定任务的内存连接；不影响其他任务或持久化 MCP 配置。"""

        if not task_id:
            return False
        with self._lock:
            sessions = [
                self._task_sessions.pop(key)
                for key in tuple(self._task_sessions)
                if key[0] == task_id
            ]
        if not sessions:
            return False
        for session in sessions:
            session.close()
        return True

    def _configuration_sources(self, workspace_root: Path) -> tuple[McpConfigurationSource, ...]:
        """枚举项目和已验证插件快照的配置，不把磁盘路径写入任务状态。"""

        sources: list[McpConfigurationSource] = []
        project_config = _config_path(workspace_root)
        if project_config.is_file():
            sources.append(McpConfigurationSource("project", project_config))
        if self._plugin_registry is not None:
            for plugin_source in self._plugin_registry.active_mcp_config_sources():
                sources.append(
                    McpConfigurationSource(
                        f"plugin:{plugin_source.plugin_id}",
                        plugin_source.config_path,
                        plugin_source.package_sha256,
                    )
                )
        return tuple(sorted(sources, key=lambda item: item.source_id))

    def _source_for_binding(
        self,
        binding: McpToolBinding,
        workspace_root: Path,
    ) -> McpConfigurationSource | None:
        """任务恢复或工具调用前复验被冻结的配置来源。"""

        if binding.config_source_id == "project":
            path = _config_path(workspace_root)
            return McpConfigurationSource("project", path) if path.is_file() else None
        if not binding.config_source_id.startswith("plugin:") or self._plugin_registry is None:
            return None
        plugin_id = binding.config_source_id.removeprefix("plugin:")
        package_sha256 = binding.plugin_package_sha256
        if package_sha256 is None:
            return None
        plugin_source = self._plugin_registry.mcp_config_source(plugin_id, package_sha256)
        if plugin_source is None:
            return None
        return McpConfigurationSource(
            binding.config_source_id,
            plugin_source.config_path,
            plugin_source.package_sha256,
        )

    def _connect_server(
        self,
        configuration: McpConfiguration,
        workspace_root: Path,
        source_id: str,
        config_sha256: str,
        server_name: str,
        permission: PermissionGrant,
        *,
        task_id: str | None,
    ) -> Any:
        if task_id:
            session = self._task_session(task_id, configuration, workspace_root, source_id, config_sha256, permission)
            return session.invoke(lambda runtime: runtime.connect(server_name, permission, approved=True))
        return _run_async(lambda: self._discover_server(configuration, workspace_root, server_name, permission))

    def _call_server(
        self,
        configuration: McpConfiguration,
        workspace_root: Path,
        binding: McpToolBinding,
        arguments: dict[str, object],
        permission: PermissionGrant,
        source_id: str,
        *,
        task_id: str | None,
    ) -> McpToolCallResult:
        if task_id:
            session = self._task_session(task_id, configuration, workspace_root, source_id, binding.config_sha256, permission)
            return session.invoke(lambda runtime: self._call_runtime(runtime, binding, arguments, permission))
        return _run_async(lambda: self._call_once(configuration, workspace_root, binding, arguments, permission))

    def _task_session(
        self,
        task_id: str,
        configuration: McpConfiguration,
        workspace_root: Path,
        source_id: str,
        config_sha256: str,
        permission: PermissionGrant,
    ) -> _TaskMcpRuntimeSession:
        if not task_id.strip() or len(task_id) > 128:
            raise ValueError("MCP_TASK_ID_INVALID")
        stale: _TaskMcpRuntimeSession | None = None
        with self._lock:
            key = (task_id, source_id)
            current = self._task_sessions.get(key)
            if current is not None and current.matches(workspace_root, source_id, config_sha256, permission.is_full_access):
                return current
            stale = self._task_sessions.pop(key, None)
        if stale is not None:
            stale.close()
        created = _TaskMcpRuntimeSession(
            self._runtime_factory,
            configuration,
            workspace_root,
            source_id,
            config_sha256,
            permission.is_full_access,
        )
        with self._lock:
            existing = self._task_sessions.get(key)
            if existing is None:
                self._task_sessions[key] = created
                return created
        created.close()
        return existing

    async def _discover_server(
        self,
        configuration: McpConfiguration,
        workspace_root: Path,
        server_name: str,
        permission: PermissionGrant,
    ) -> Any:
        runtime = self._runtime_factory(configuration, workspace_root)
        try:
            return await runtime.connect(server_name, permission, approved=True)
        finally:
            await runtime.close()

    async def _call_once(
        self,
        configuration: McpConfiguration,
        workspace_root: Path,
        binding: McpToolBinding,
        arguments: dict[str, object],
        permission: PermissionGrant,
    ) -> McpToolCallResult:
        runtime = self._runtime_factory(configuration, workspace_root)
        try:
            return await self._call_runtime(runtime, binding, arguments, permission)
        finally:
            await runtime.close()

    @staticmethod
    async def _call_runtime(
        runtime: McpRuntime,
        binding: McpToolBinding,
        arguments: dict[str, object],
        permission: PermissionGrant,
    ) -> McpToolCallResult:
        connected = await runtime.connect(binding.server_name, permission, approved=True)
        if connected.status != "READY":
            return _blocked_runtime_call(connected.code, binding.server_name, binding.capability_id)
        descriptor = runtime.capabilities.capabilities.get(binding.capability_id)
        schema = descriptor.metadata.get("input_schema") if descriptor is not None else None
        if not isinstance(schema, dict) or _sha256_json(schema) != binding.schema_sha256:
            return _blocked_runtime_call("MCP_TOOL_CHANGED_AFTER_DISCOVERY", binding.server_name, binding.capability_id)
        return await runtime.call_tool(binding.capability_id, arguments, permission, approved=True)

    def _langchain_tool(
        self,
        binding: McpToolBinding,
        permission: PermissionGrant,
        workspace_root: Path,
        *,
        task_id: str | None,
        artifact_output_root: Path | None,
        artifact_task_id: str | None,
    ) -> StructuredTool:
        arguments_model = _arguments_model(binding)

        def invoke_mcp(**arguments: object) -> dict[str, object]:
            # 动态 Tool 只接受 schema 中的字段，实际 Schema 仍由 MCP Runtime 二次校验。
            return self.invoke_in_workspace(
                binding,
                dict(arguments),
                permission,
                workspace_root,
                task_id=task_id,
                artifact_output_root=artifact_output_root,
                artifact_task_id=artifact_task_id,
            )

        return StructuredTool.from_function(
            invoke_mcp,
            name=binding.capability_id,
            description=f"外部 MCP 只读工具（不可信输出）：{binding.description}",
            args_schema=arguments_model,
        )


def bindings_registry(bindings: Iterable[McpToolBinding]) -> CapabilityRegistry:
    return CapabilityRegistry(binding.descriptor() for binding in bindings)


def _arguments_model(binding: McpToolBinding) -> type[Any]:
    properties = binding.input_schema.get("properties", {})
    required = binding.input_schema.get("required", [])
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise ValueError("MCP_TOOL_SCHEMA_INVALID")
    fields: dict[str, tuple[type[Any], object]] = {}
    for name, schema in properties.items():
        if not isinstance(name, str) or not isinstance(schema, dict):
            raise ValueError("MCP_TOOL_SCHEMA_INVALID")
        field_type = _json_schema_type(schema)
        fields[name] = (field_type, ... if name in required else None)
    return create_model(
        "McpArguments_" + hashlib.sha256(binding.capability_id.encode("utf-8")).hexdigest()[:12],
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )


def _json_schema_type(schema: dict[str, object]) -> type[Any]:
    schema_type = schema.get("type")
    return {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": list,
        "object": dict,
    }.get(schema_type, Any)


def _valid_object_schema(schema: dict[str, object]) -> bool:
    if schema.get("type") != "object":
        return False
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(properties, dict) or not isinstance(required, list):
        return False
    return all(isinstance(name, str) and isinstance(item, dict) for name, item in properties.items()) and all(
        isinstance(name, str) and name in properties for name in required
    )


def _config_path(workspace_root: Path) -> Path:
    root = workspace_root.expanduser().resolve()
    path = (root / MCP_CONFIG_RELATIVE_PATH).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError("MCP_CONFIG_PATH_ESCAPE") from error
    return path


def _blocked(code: str, message: str) -> dict[str, object]:
    return {"status": "BLOCKED", "code": code, "message": message, "data": {}}


def _blocked_runtime_call(code: str, server_name: str, tool_name: str) -> McpToolCallResult:
    """将连接或 Schema 阻断保持为运行时领域结果，避免伪造外部工具数据。"""

    return McpToolCallResult(
        status="BLOCKED",
        code=code,
        server_name=server_name,
        tool_name=tool_name,
        data={},
        truncated=False,
        output_sha256=_sha256_bytes(b"{}"),
        original_chars=0,
        duration_ms=0,
    )


def _bound_agent_result(payload: dict[str, object]) -> dict[str, object]:
    """MCP 自身的上限可更大，但研究模型上下文必须有独立硬上限。"""

    data = payload.get("data")
    try:
        serialized = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return _blocked("MCP_TOOL_OUTPUT_INVALID", "MCP 工具返回了无法安全序列化的结果。")
    if len(serialized) <= MAX_AGENT_MCP_RESULT_CHARS:
        return payload
    bounded = dict(payload)
    bounded["data"] = {"preview": serialized[:MAX_AGENT_MCP_RESULT_CHARS], "content_omitted": True}
    bounded["truncated"] = True
    bounded["agent_output_truncated"] = True
    bounded["agent_original_chars"] = len(serialized)
    return bounded


def _persist_large_mcp_output(
    result: McpToolCallResult,
    *,
    output_root: Path | None,
    task_id: str | None,
) -> dict[str, object] | None:
    """把超过模型上下文上限的原始 MCP 输出写入任务目录，状态中只保留可核验引用。"""

    content = result.artifact_content
    if content is None or result.original_chars <= MAX_AGENT_MCP_RESULT_CHARS:
        return None
    if result.original_chars > MAX_MCP_TASK_ARTIFACT_CHARS:
        return {
            "status": "OMITTED",
            "code": "MCP_ARTIFACT_TOO_LARGE",
            "output_sha256": result.output_sha256,
            "original_chars": result.original_chars,
            "max_chars": MAX_MCP_TASK_ARTIFACT_CHARS,
        }
    if output_root is None or not task_id or not _SAFE_TASK_ID.fullmatch(task_id) or task_id in {".", ".."}:
        return {
            "status": "UNAVAILABLE",
            "code": "MCP_ARTIFACT_CONTEXT_INVALID",
            "output_sha256": result.output_sha256,
            "original_chars": result.original_chars,
        }
    content_bytes = content.encode("utf-8")
    if len(content_bytes) > MAX_MCP_TASK_ARTIFACT_CHARS:
        return {
            "status": "OMITTED",
            "code": "MCP_ARTIFACT_TOO_LARGE",
            "output_sha256": result.output_sha256,
            "original_chars": result.original_chars,
            "max_chars": MAX_MCP_TASK_ARTIFACT_CHARS,
        }
    digest = _sha256_bytes(content_bytes)
    if digest != result.output_sha256:
        return {
            "status": "UNAVAILABLE",
            "code": "MCP_ARTIFACT_HASH_MISMATCH",
            "output_sha256": result.output_sha256,
            "original_chars": result.original_chars,
        }
    try:
        root = output_root.expanduser().resolve()
        directory = (root / task_id).resolve()
        destination = (directory / "mcp" / "outputs" / f"{digest}.json").resolve()
        destination.relative_to(directory)
        _atomic_write_bytes(destination, content_bytes)
    except OSError:
        return {
            "status": "UNAVAILABLE",
            "code": "MCP_ARTIFACT_WRITE_FAILED",
            "output_sha256": result.output_sha256,
            "original_chars": result.original_chars,
        }
    return {
        "status": "READY",
        "kind": "mcp_tool_output",
        "relative_path": destination.relative_to(directory).as_posix(),
        "sha256": digest,
        "size_bytes": len(content_bytes),
        "server_name": result.server_name,
        "tool_name": result.tool_name,
        "output_sha256": result.output_sha256,
        "original_chars": result.original_chars,
    }


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    """用同目录临时文件替换，避免 Agent 被取消时留下半截 MCP 原文。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: object) -> str:
    return _sha256_bytes(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _run_async(operation: Callable[[], Awaitable[_T]]) -> _T:
    """同步图节点可调用异步 MCP Runtime；已有事件循环时使用专用线程。"""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(operation())

    result: list[_T] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            result.append(asyncio.run(operation()))
        except BaseException as error:  # 线程边界必须把异常带回同步调用方。
            errors.append(error)

    thread = Thread(target=run, name="repopilot-mcp-bridge", daemon=True)
    thread.start()
    thread.join()
    if errors:
        raise errors[0]
    return result[0]
