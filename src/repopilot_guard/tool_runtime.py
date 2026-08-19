"""受控工具的统一注册、权限校验、预算与输出边界。

执行管线为双层结构（阶段四 Step 1「ToolRuntime 双层管线」）：

- **pre-execute 钩子链（瀑布策略）**：可组合的策略层。每个钩子返回
  allow / deny / ask；任一钩子 deny 即短路，ask 需要审批服务裁决。
- **单调守卫（monotonic guard）**：钩子链放行之后由内核再次调用。
  守卫只能拒绝，任何钩子都无法放开被守卫拒绝的调用——策略可以讨论，
  底线不容商量。
- **审批降级语义**：ask 无审批服务 / 无回答 / 被取消一律拒绝；
  拒绝原因区分「用户拒绝」与「审批通道不可用」。
- **post-execute 钩子链**：对执行结果做 accept（可替换 payload）或
  block（转为纠错反馈）。

钩子与守卫的注册均返回撤销器（注册即副作用，卸载即撤销）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Iterable, Protocol

from langchain_core.tools import StructuredTool
from pydantic import ValidationError

from repopilot_guard.capabilities import CapabilityPolicy, CapabilityRegistry
from repopilot_guard.permissions import PermissionGrant
from repopilot_guard.policy import PolicyDecision


DEFAULT_TOOL_TIMEOUT_SECONDS = 30
DEFAULT_TOOL_OUTPUT_CHARS = 32 * 1024


class PreToolKind(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class PostToolKind(StrEnum):
    ACCEPT = "accept"
    BLOCK = "block"


class ApprovalOutcome(StrEnum):
    """审批服务对一次 ask 的裁决；对齐"无回答方即拒"的降级语义。"""

    ALLOWED_ONCE = "allowed-once"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    UNAVAILABLE = "unavailable"


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
class ToolExecution:
    """一次待执行调用的上下文快照；钩子与守卫只读，不得改写参数。"""

    name: str
    arguments: dict[str, object]
    definition: ToolDefinition


@dataclass(frozen=True, slots=True)
class PreToolDecision:
    """pre-execute 钩子的三分决策（对齐 dsh PreToolDecision）。"""

    kind: PreToolKind
    code: str = ""
    reason: str = ""

    @staticmethod
    def allow() -> "PreToolDecision":
        return PreToolDecision(PreToolKind.ALLOW)

    @staticmethod
    def deny(code: str, reason: str) -> "PreToolDecision":
        return PreToolDecision(PreToolKind.DENY, code=code, reason=reason)

    @staticmethod
    def ask(reason: str) -> "PreToolDecision":
        return PreToolDecision(PreToolKind.ASK, reason=reason)


@dataclass(frozen=True, slots=True)
class PostToolDecision:
    """post-execute 钩子的决策；accept 可选替换 payload，block 转为纠错反馈。"""

    kind: PostToolKind
    code: str = ""
    reason: str = ""
    payload: dict[str, object] | None = None

    @staticmethod
    def accept(payload: dict[str, object] | None = None) -> "PostToolDecision":
        return PostToolDecision(PostToolKind.ACCEPT, payload=payload)

    @staticmethod
    def block(code: str, reason: str) -> "PostToolDecision":
        return PostToolDecision(PostToolKind.BLOCK, code=code, reason=reason)


class PreExecuteHook(Protocol):
    def __call__(self, execution: ToolExecution) -> PreToolDecision: ...


class PostExecuteHook(Protocol):
    def __call__(self, execution: ToolExecution, result: ToolInvocationResult) -> PostToolDecision: ...


class RuntimeGuard(Protocol):
    """单调守卫：返回拒绝时生效，返回 None 表示放行；钩子无法覆盖守卫拒绝。"""

    def __call__(self, execution: ToolExecution) -> PolicyDecision | None: ...


class ApprovalService(Protocol):
    def request(self, execution: ToolExecution, reason: str) -> ApprovalOutcome: ...


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
    """只允许已注册 Structured Tool，并在模型外执行权限和输出预算控制。

    管线：注册/预算/能力白名单 → pre-execute 钩子链 → 审批降级 →
    单调守卫 → 工具体执行 → post-execute 钩子链 → 输出预算与截断。
    """

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
        pre_hooks: Iterable[PreExecuteHook] = (),
        post_hooks: Iterable[PostExecuteHook] = (),
        guards: Iterable[RuntimeGuard] = (),
        approval_service: ApprovalService | None = None,
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
        self._pre_hooks: list[PreExecuteHook] = list(pre_hooks)
        self._post_hooks: list[PostExecuteHook] = list(post_hooks)
        self._guards: list[RuntimeGuard] = list(guards)
        self._approval_service = approval_service

    # ------------------------------------------------------------------
    # 注册即副作用：所有插槽注册返回撤销器，卸载即恢复原状
    # ------------------------------------------------------------------

    def add_pre_execute_hook(self, hook: PreExecuteHook) -> Callable[[], None]:
        self._pre_hooks.append(hook)
        return _remover(self._pre_hooks, hook)

    def add_post_execute_hook(self, hook: PostExecuteHook) -> Callable[[], None]:
        self._post_hooks.append(hook)
        return _remover(self._post_hooks, hook)

    def add_guard(self, guard: RuntimeGuard) -> Callable[[], None]:
        """注册单调守卫：只降不升，钩子无法放开守卫拒绝的调用。"""
        self._guards.append(guard)
        return _remover(self._guards, guard)

    @property
    def approval_service(self) -> ApprovalService | None:
        return self._approval_service

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
        execution = ToolExecution(name=name, arguments=dict(arguments), definition=definition)

        # ① pre-execute 钩子链（瀑布策略）：任一 deny 短路；ask 记入待裁决。
        ask_reason: str | None = None
        for hook in tuple(self._pre_hooks):
            decision = hook(execution)
            if decision.kind is PreToolKind.DENY:
                return self._blocked(decision.code or "TOOL_POLICY_BLOCKED", decision.reason or "工具调用被策略拒绝。", duration_ms=_duration_ms(started), definition=definition)
            if decision.kind is PreToolKind.ASK and ask_reason is None:
                ask_reason = decision.reason or "工具调用需要审批。"

        # ② 审批降级：无审批通道一律拒绝，且区分"用户拒绝"与"通道不可用"。
        if ask_reason is not None:
            outcome = self._resolve_ask(execution, ask_reason)
            if outcome is not ApprovalOutcome.ALLOWED_ONCE:
                code, message = _approval_denial(outcome, name)
                return self._blocked(code, message, duration_ms=_duration_ms(started), definition=definition)

        # ③ 单调守卫：钩子放行之后由内核复查；守卫拒绝不可被任何钩子覆盖。
        for guard in tuple(self._guards):
            verdict = guard(execution)
            if verdict is not None and not verdict.allowed:
                return self._blocked(verdict.audit_code or "POLICY_BLOCKED", verdict.reason or "工具调用被安全底线拒绝。", duration_ms=_duration_ms(started), definition=definition)

        # ④ 工具体执行。
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

        # ⑤ post-execute 钩子链：block 转为纠错反馈；accept 可替换 payload。
        provisional = ToolInvocationResult(payload, _duration_ms(started), 0, False, definition)
        for hook in tuple(self._post_hooks):
            decision = hook(execution, provisional)
            if decision.kind is PostToolKind.BLOCK:
                return self._blocked(decision.code or "TOOL_POST_BLOCKED", decision.reason or "工具结果被后处理策略拒绝。", duration_ms=_duration_ms(started), definition=definition)
            if decision.payload is not None:
                payload = decision.payload
                provisional = ToolInvocationResult(payload, provisional.duration_ms, 0, False, definition)

        # ⑥ 输出预算与截断。
        remaining = None if self._max_total_output_chars is None else max(0, self._max_total_output_chars - self._output_chars)
        if remaining == 0:
            return self._blocked("TOOL_OUTPUT_BUDGET_REACHED", "工具输出预算已耗尽。", duration_ms=_duration_ms(started), definition=definition)
        limit = definition.max_output_chars if remaining is None else min(definition.max_output_chars, remaining)
        limited_payload, output_chars, truncated = _limit_output(payload, limit)
        self._output_chars += output_chars
        return ToolInvocationResult(limited_payload, _duration_ms(started), output_chars, truncated, definition)

    def _resolve_ask(self, execution: ToolExecution, reason: str) -> ApprovalOutcome:
        if self._approval_service is None:
            return ApprovalOutcome.UNAVAILABLE
        return self._approval_service.request(execution, reason)

    @staticmethod
    def _blocked(
        code: str,
        message: str,
        *,
        duration_ms: int = 0,
        definition: ToolDefinition | None = None,
    ) -> ToolInvocationResult:
        return ToolInvocationResult({"status": "BLOCKED", "code": code, "message": message, "data": {}}, duration_ms, 0, False, definition)


def _approval_denial(outcome: ApprovalOutcome, name: str) -> tuple[str, str]:
    """把审批裁决映射为明确的拒绝语义：模型能区分"人说不行"与"找不到人问"。"""
    if outcome is ApprovalOutcome.REJECTED:
        return "TOOL_APPROVAL_REJECTED", f"用户拒绝了工具「{name}」。"
    if outcome is ApprovalOutcome.CANCELLED:
        return "TOOL_APPROVAL_CANCELLED", f"工具「{name}」的审批已被取消。"
    return "APPROVAL_UNAVAILABLE", f"工具「{name}」需要审批，但当前没有可用的审批通道。"


def _remover(hooks: list, item) -> Callable[[], None]:
    def dispose() -> None:
        try:
            hooks.remove(item)
        except ValueError:
            pass

    return dispose


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
