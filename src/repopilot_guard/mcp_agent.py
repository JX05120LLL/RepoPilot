"""将经任务授权的只读 MCP 工具安全接入研究图。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from threading import Thread
from typing import Any, TypeVar

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
from repopilot_guard.mcp_runtime import McpRuntime
from repopilot_guard.permissions import PermissionGrant


MCP_CONFIG_RELATIVE_PATH = Path(".repopilot") / "mcp.toml"
MAX_AGENT_MCP_RESULT_CHARS = 20_000
_T = TypeVar("_T")
McpRuntimeFactory = Callable[[McpConfiguration, Path], McpRuntime]


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

    @classmethod
    def from_descriptor(cls, descriptor: CapabilityDescriptor, config_sha256: str) -> "McpToolBinding":
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
            source=f"mcp:{self.server_name}",
            risks=frozenset(CapabilityRisk(item) for item in self.risks),
            metadata={"server": self.server_name, "input_schema": self.input_schema, "frozen": True},
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

    def __init__(self, runtime_factory: McpRuntimeFactory | None = None) -> None:
        self._runtime_factory = runtime_factory or (lambda configuration, root: McpRuntime(configuration, workspace_root=root))

    def discover(
        self,
        workspace_root: Path,
        permission: PermissionGrant,
        approved_mcp_tools: Iterable[str] = (),
    ) -> McpBindingResult:
        approved = frozenset(approved_mcp_tools)
        config_path = _config_path(workspace_root)
        if not permission.is_full_access and not approved:
            return McpBindingResult("READY", "MCP_NOT_REQUESTED", "安全模式未显式批准 MCP 工具，未建立外部连接。")
        if not config_path.is_file():
            if approved:
                return McpBindingResult("BLOCKED", "MCP_CONFIG_NOT_FOUND", "已批准 MCP 工具，但项目未提供 MCP 配置。")
            return McpBindingResult("READY", "MCP_NOT_CONFIGURED", "项目未配置 MCP，继续使用内置研究工具。")
        try:
            configuration = McpConfigLoader.load(config_path)
            config_sha256 = _sha256_bytes(config_path.read_bytes())
        except McpConfigError as error:
            return McpBindingResult("BLOCKED", error.code, "MCP 配置无效，未建立外部连接。")
        except OSError:
            return McpBindingResult("BLOCKED", "MCP_CONFIG_READ_FAILED", "MCP 配置无法读取，未建立外部连接。")

        bindings: list[McpToolBinding] = []
        issues: list[dict[str, str]] = []
        discovered_ids: set[str] = set()
        for server in sorted(configuration.servers, key=lambda item: item.name):
            requested_for_server = tuple(item for item in approved if item.startswith(f"mcp__{server.name}__"))
            if not permission.is_full_access and not requested_for_server:
                continue
            if server.access is not McpAccess.READ_ONLY:
                issues.append({"server_name": server.name, "code": "MCP_WRITE_SERVER_NOT_RESEARCH_BINDABLE"})
                continue
            try:
                result = _run_async(lambda: self._discover_server(configuration, workspace_root, server.name, permission))
            except Exception:
                issues.append({"server_name": server.name, "code": "MCP_DISCOVERY_UNAVAILABLE"})
                continue
            if result.status != "READY":
                issues.append({"server_name": server.name, "code": result.code})
                continue
            for descriptor in result.tools:
                if not permission.is_full_access and descriptor.capability_id not in approved:
                    continue
                try:
                    binding = McpToolBinding.from_descriptor(descriptor, config_sha256)
                except ValueError:
                    issues.append({"server_name": server.name, "code": "MCP_TOOL_BINDING_METADATA_INVALID"})
                    continue
                bindings.append(binding)
                discovered_ids.add(binding.capability_id)

        missing = sorted(approved - discovered_ids)
        if missing:
            return McpBindingResult(
                "BLOCKED",
                "MCP_APPROVED_TOOL_NOT_DISCOVERED",
                "已批准的 MCP 工具未被当前配置和服务发现，任务不会扩大到其他工具。",
                tuple(sorted(bindings, key=lambda item: item.capability_id)),
                tuple(issues),
            )
        if not bindings:
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
    ) -> tuple[StructuredTool, ...]:
        root = workspace_root.expanduser().resolve()
        return tuple(self._langchain_tool(binding, permission, root) for binding in bindings)

    def invoke_in_workspace(
        self,
        binding: McpToolBinding,
        arguments: dict[str, object],
        permission: PermissionGrant,
        workspace_root: Path,
    ) -> dict[str, object]:
        config_path = _config_path(workspace_root)
        try:
            if _sha256_bytes(config_path.read_bytes()) != binding.config_sha256:
                return _blocked("MCP_CONFIG_CHANGED_AFTER_DISCOVERY", "MCP 配置在任务开始后发生变化，已阻断调用。")
            configuration = McpConfigLoader.load(config_path)
        except McpConfigError as error:
            return _blocked(error.code, "MCP 配置无效，已阻断调用。")
        except OSError:
            return _blocked("MCP_CONFIG_READ_FAILED", "MCP 配置无法读取，已阻断调用。")
        try:
            result = _run_async(
                lambda: self._call_once(configuration, workspace_root, binding, arguments, permission)
            )
        except Exception:
            return _blocked("MCP_TOOL_CALL_UNAVAILABLE", "MCP 工具调用不可用，未返回伪造结果。")
        return _bound_agent_result(result)

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
    ) -> dict[str, object]:
        runtime = self._runtime_factory(configuration, workspace_root)
        try:
            connected = await runtime.connect(binding.server_name, permission, approved=True)
            if connected.status != "READY":
                return _blocked(connected.code, "MCP 服务无法连接，未执行工具调用。")
            descriptor = runtime.capabilities.capabilities.get(binding.capability_id)
            schema = descriptor.metadata.get("input_schema") if descriptor is not None else None
            if not isinstance(schema, dict) or _sha256_json(schema) != binding.schema_sha256:
                return _blocked("MCP_TOOL_CHANGED_AFTER_DISCOVERY", "MCP 工具 Schema 在任务开始后发生变化，已阻断调用。")
            result = await runtime.call_tool(binding.capability_id, arguments, permission, approved=True)
            payload = result.to_dict()
            payload["message"] = "MCP 工具调用完成。" if result.status == "READY" else "MCP 工具未成功完成。"
            return payload
        finally:
            await runtime.close()

    def _langchain_tool(
        self,
        binding: McpToolBinding,
        permission: PermissionGrant,
        workspace_root: Path,
    ) -> StructuredTool:
        arguments_model = _arguments_model(binding)

        def invoke_mcp(**arguments: object) -> dict[str, object]:
            # 动态 Tool 只接受 schema 中的字段，实际 Schema 仍由 MCP Runtime 二次校验。
            return self.invoke_in_workspace(binding, dict(arguments), permission, workspace_root)

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
