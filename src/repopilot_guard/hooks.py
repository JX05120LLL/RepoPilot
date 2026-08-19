"""签名插件的声明式 Hook 聚合器。

Hook 不是脚本执行框架。它只读取已验证插件快照中的固定声明，返回可审计的
allow/ask/deny 建议。PolicyGuard、权限快照和审批仍是最终强制边界。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from repopilot_guard.plugins import PluginHook, PluginRegistry


class HookEvent(str, Enum):
    """首版只在固定 Agent 阶段读取 Hook，避免插件定义任意触发点。"""

    TASK_INTAKE = "task_intake"
    PLAN_APPROVAL = "plan_approval"
    EXECUTION_APPROVAL = "execution_approval"


class HookDecision(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class HookOutcome:
    plugin_id: str
    hook_id: str
    event: HookEvent
    decision: HookDecision
    message: str
    context: dict[str, str]

    def to_dict(self) -> dict[str, object]:
        return {
            "plugin_id": self.plugin_id,
            "hook_id": self.hook_id,
            "event": self.event.value,
            "decision": self.decision.value,
            "message": self.message,
            "context": dict(self.context),
        }


@dataclass(frozen=True, slots=True)
class HookEvaluation:
    event: HookEvent
    decision: HookDecision
    outcomes: tuple[HookOutcome, ...]

    @property
    def is_denied(self) -> bool:
        return self.decision is HookDecision.DENY

    @property
    def requires_confirmation(self) -> bool:
        return self.decision is HookDecision.ASK

    def to_event(self) -> dict[str, object]:
        return {
            "type": "DECLARATIVE_HOOKS_EVALUATED",
            "event": self.event.value,
            "decision": self.decision.value,
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
        }


class HookRuntime:
    """从当前可信插件快照中确定性聚合 Hook，绝不执行第三方代码。"""

    def __init__(self, plugin_registry: PluginRegistry | None = None) -> None:
        self._plugin_registry = plugin_registry

    def evaluate(self, event: HookEvent) -> HookEvaluation:
        if self._plugin_registry is None:
            return HookEvaluation(event=event, decision=HookDecision.ALLOW, outcomes=())
        outcomes = tuple(
            HookOutcome(
                plugin_id=hook.plugin_id,
                hook_id=hook.hook_id,
                event=event,
                decision=HookDecision(hook.decision),
                message=hook.message,
                context=dict(hook.context),
            )
            for hook in self._plugin_registry.active_hooks()
            if hook.event == event.value
        )
        # deny 只会额外收紧；allow 永远不会替代 PolicyGuard 或用户审批。
        decision = HookDecision.DENY if any(item.decision is HookDecision.DENY for item in outcomes) else (
            HookDecision.ASK if any(item.decision is HookDecision.ASK for item in outcomes) else HookDecision.ALLOW
        )
        return HookEvaluation(event=event, decision=decision, outcomes=outcomes)


def hook_event_from_name(value: str) -> HookEvent:
    """将 checkpoint 中的事件名重新约束回固定枚举。"""

    return HookEvent(value)
