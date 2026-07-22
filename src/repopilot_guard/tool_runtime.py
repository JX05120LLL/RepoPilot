"""受控工具的统一注册与调用入口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from langchain_core.tools import StructuredTool
from pydantic import ValidationError

from repopilot_guard.capabilities import CapabilityPolicy, CapabilityRegistry
from repopilot_guard.permissions import PermissionGrant


@dataclass(frozen=True, slots=True)
class ToolInvocationResult:
    """工具调用结果；完整 payload 只返回给图，不直接写入审计摘要。"""

    payload: dict[str, object]

    @property
    def status(self) -> str:
        return str(self.payload.get("status", "READY"))

    @property
    def code(self) -> str:
        return str(self.payload.get("code", "TOOL_COMPLETED"))


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """工具的安全元数据；执行层据此保持超时和审计边界可见。"""

    name: str
    risk_category: str
    timeout_seconds: int
    max_output_chars: int


class ToolRuntime:
    """只允许调用显式注册的 Structured Tool，并统一处理参数错误。"""

    def __init__(
        self,
        tools: Iterable[StructuredTool],
        definitions: Iterable[ToolDefinition] = (),
        *,
        capabilities: CapabilityRegistry | None = None,
        permission: PermissionGrant | None = None,
        approved_capabilities: Iterable[str] = (),
        capability_policy: CapabilityPolicy | None = None,
    ) -> None:
        registered = tuple(tools)
        names = [tool.name for tool in registered]
        if len(names) != len(set(names)):
            raise ValueError("DUPLICATE_TOOL_REGISTRATION")
        self._tools = {tool.name: tool for tool in registered}
        self._definitions = {item.name: item for item in definitions}
        self._capabilities = capabilities
        self._permission = permission or PermissionGrant.safe()
        self._approved_capabilities = frozenset(approved_capabilities)
        self._capability_policy = capability_policy or CapabilityPolicy()

    @property
    def langchain_tools(self) -> tuple[StructuredTool, ...]:
        return tuple(self._tools.values())

    def definition(self, name: str) -> ToolDefinition | None:
        return self._definitions.get(name)

    def invoke(self, name: str, arguments: dict[str, object]) -> ToolInvocationResult:
        tool = self._tools.get(name)
        if tool is None:
            return self._blocked("TOOL_NOT_ALLOWLISTED", "工具未注册，已拒绝。")
        if self._capabilities is not None:
            descriptor = self._capabilities.get(name)
            if descriptor is None:
                return self._blocked("CAPABILITY_NOT_REGISTERED", "工具缺少能力清单记录，已拒绝。")
            decision = self._capability_policy.decide(
                descriptor,
                self._permission,
                approved=name in self._approved_capabilities,
            )
            if not decision.allowed:
                return self._blocked(decision.code, decision.reason)
        if not isinstance(arguments, dict):
            return self._blocked("INVALID_TOOL_ARGUMENTS", "工具参数必须是对象。")
        try:
            result = tool.invoke(arguments)
        except (TypeError, ValueError, ValidationError):
            return self._blocked("INVALID_TOOL_ARGUMENTS", "工具参数不合法。")
        if isinstance(result, dict):
            return ToolInvocationResult(result)
        return ToolInvocationResult(
            {
                "status": "READY",
                "code": "TOOL_COMPLETED",
                "message": "工具执行完成。",
                "data": {"result": result},
            }
        )

    @staticmethod
    def _blocked(code: str, message: str) -> ToolInvocationResult:
        return ToolInvocationResult({"status": "BLOCKED", "code": code, "message": message, "data": {}})
