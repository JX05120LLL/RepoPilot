"""受控工具的统一注册、权限校验、预算与输出边界。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Iterable

from langchain_core.tools import StructuredTool
from pydantic import ValidationError

from repopilot_guard.capabilities import CapabilityPolicy, CapabilityRegistry
from repopilot_guard.permissions import PermissionGrant


DEFAULT_TOOL_TIMEOUT_SECONDS = 30
DEFAULT_TOOL_OUTPUT_CHARS = 32 * 1024


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """工具的执行契约；Shell/MCP 等具体运行时再负责实际超时和取消。"""

    name: str
    risk_category: str
    timeout_seconds: int
    max_output_chars: int

    def __post_init__(self) -> None:
        if not self.name or self.timeout_seconds < 1 or self.max_output_chars < 1:
            raise ValueError("INVALID_TOOL_DEFINITION")


@dataclass(frozen=True, slots=True)
class ToolInvocationResult:
    """完整 payload 只回填给图；审计使用独立的脱敏摘要与遥测字段。"""

    payload: dict[str, object]
    duration_ms: int = 0
    output_chars: int = 0
    output_truncated: bool = False
    definition: ToolDefinition | None = None

    @property
    def status(self) -> str:
        return str(self.payload.get("status", "READY"))

    @property
    def code(self) -> str:
        return str(self.payload.get("code", "TOOL_COMPLETED"))


class ToolRuntime:
    """只允许已注册 Structured Tool，并在模型外执行权限和输出预算控制。"""

    def __init__(
        self,
        tools: Iterable[StructuredTool],
        definitions: Iterable[ToolDefinition] = (),
        *,
        capabilities: CapabilityRegistry | None = None,
        permission: PermissionGrant | None = None,
        approved_capabilities: Iterable[str] = (),
        capability_policy: CapabilityPolicy | None = None,
        max_invocations: int | None = None,
        max_total_output_chars: int | None = None,
    ) -> None:
        registered = tuple(tools)
        names = [tool.name for tool in registered]
        if len(names) != len(set(names)):
            raise ValueError("DUPLICATE_TOOL_REGISTRATION")
        self._tools = {tool.name: tool for tool in registered}
        self._definitions = {item.name: item for item in definitions}
        if any(name not in self._tools for name in self._definitions):
            raise ValueError("TOOL_DEFINITION_NOT_REGISTERED")
        self._capabilities = capabilities
        self._permission = permission or PermissionGrant.safe()
        self._approved_capabilities = frozenset(approved_capabilities)
        self._capability_policy = capability_policy or CapabilityPolicy()
        if max_invocations is not None and max_invocations < 1:
            raise ValueError("INVALID_TOOL_INVOCATION_BUDGET")
        if max_total_output_chars is not None and max_total_output_chars < 1:
            raise ValueError("INVALID_TOOL_OUTPUT_BUDGET")
        self._max_invocations = max_invocations
        self._max_total_output_chars = max_total_output_chars
        self._invocation_count = 0
        self._output_chars = 0

    @property
    def langchain_tools(self) -> tuple[StructuredTool, ...]:
        return tuple(self._tools.values())

    def definition(self, name: str) -> ToolDefinition | None:
        if name not in self._tools:
            return None
        return self._definitions.get(name) or ToolDefinition(
            name=name,
            risk_category="unknown",
            timeout_seconds=DEFAULT_TOOL_TIMEOUT_SECONDS,
            max_output_chars=DEFAULT_TOOL_OUTPUT_CHARS,
        )

    def invoke(self, name: str, arguments: dict[str, object]) -> ToolInvocationResult:
        tool = self._tools.get(name)
        if tool is None:
            return self._blocked("TOOL_NOT_ALLOWLISTED", "工具未注册，已拒绝。")
        definition = self.definition(name)
        assert definition is not None
        if self._max_invocations is not None and self._invocation_count >= self._max_invocations:
            return self._blocked("TOOL_INVOCATION_BUDGET_REACHED", "工具调用次数已达到当前节点预算。", definition=definition)
        self._invocation_count += 1
        if self._capabilities is not None:
            descriptor = self._capabilities.get(name)
            if descriptor is None:
                return self._blocked("CAPABILITY_NOT_REGISTERED", "工具缺少能力清单记录，已拒绝。", definition=definition)
            decision = self._capability_policy.decide(
                descriptor,
                self._permission,
                approved=name in self._approved_capabilities,
            )
            if not decision.allowed:
                return self._blocked(decision.code, decision.reason, definition=definition)
        if not isinstance(arguments, dict):
            return self._blocked("INVALID_TOOL_ARGUMENTS", "工具参数必须是对象。", definition=definition)

        started = time.monotonic()
        try:
            raw = tool.invoke(arguments)
        except (TypeError, ValueError, ValidationError):
            return self._blocked("INVALID_TOOL_ARGUMENTS", "工具参数不合法。", duration_ms=_duration_ms(started), definition=definition)
        payload = raw if isinstance(raw, dict) else {
            "status": "READY",
            "code": "TOOL_COMPLETED",
            "message": "工具执行完成。",
            "data": {"result": raw},
        }
        remaining = None if self._max_total_output_chars is None else max(0, self._max_total_output_chars - self._output_chars)
        if remaining == 0:
            return self._blocked("TOOL_OUTPUT_BUDGET_REACHED", "工具输出预算已耗尽。", duration_ms=_duration_ms(started), definition=definition)
        limit = definition.max_output_chars if remaining is None else min(definition.max_output_chars, remaining)
        limited_payload, output_chars, truncated = _limit_output(payload, limit)
        self._output_chars += output_chars
        return ToolInvocationResult(limited_payload, _duration_ms(started), output_chars, truncated, definition)

    @staticmethod
    def _blocked(
        code: str,
        message: str,
        *,
        duration_ms: int = 0,
        definition: ToolDefinition | None = None,
    ) -> ToolInvocationResult:
        return ToolInvocationResult({"status": "BLOCKED", "code": code, "message": message, "data": {}}, duration_ms, 0, False, definition)


def _duration_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _limit_output(payload: dict[str, object], limit: int) -> tuple[dict[str, object], int, bool]:
    """超出预算时保留状态和错误信息，移除会膨胀上下文的大块正文。"""

    serialized = json.dumps(payload, ensure_ascii=False, default=str)
    if len(serialized) <= limit:
        return payload, len(serialized), False
    message = payload.get("message")
    limited: dict[str, object] = {
        "status": payload.get("status", "READY"),
        "code": payload.get("code", "TOOL_OUTPUT_TRUNCATED"),
        "message": str(message)[: min(2_000, max(0, limit // 2))] if message is not None else "工具输出已按预算截断。",
        "data": {"truncated": True, "original_output_chars": len(serialized)},
        "truncated": True,
    }
    return limited, len(json.dumps(limited, ensure_ascii=False)), True
