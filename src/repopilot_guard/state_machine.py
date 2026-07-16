"""单个仓库维护任务的显式生命周期规则。"""

from __future__ import annotations

from dataclasses import dataclass, field

from repopilot_guard.models import TaskState


class InvalidTransition(ValueError):
    """任务试图跳过既定生命周期时抛出。"""


ALLOWED_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.PREFLIGHT: frozenset({TaskState.UNDERSTAND, TaskState.BLOCKED, TaskState.FAILED}),
    TaskState.UNDERSTAND: frozenset({TaskState.LOCATE, TaskState.BLOCKED, TaskState.FAILED}),
    TaskState.LOCATE: frozenset({TaskState.PLAN, TaskState.BLOCKED, TaskState.FAILED}),
    TaskState.PLAN: frozenset({TaskState.PATCH, TaskState.REVIEW, TaskState.BLOCKED, TaskState.FAILED}),
    TaskState.PATCH: frozenset({TaskState.TEST, TaskState.REVIEW, TaskState.BLOCKED, TaskState.FAILED}),
    TaskState.TEST: frozenset({TaskState.REPAIR, TaskState.REVIEW, TaskState.BLOCKED, TaskState.FAILED}),
    TaskState.REPAIR: frozenset({TaskState.PATCH, TaskState.REVIEW, TaskState.BLOCKED, TaskState.FAILED}),
    TaskState.REVIEW: frozenset({TaskState.REPORT, TaskState.BLOCKED, TaskState.FAILED}),
    TaskState.REPORT: frozenset(),
    TaskState.BLOCKED: frozenset(),
    TaskState.FAILED: frozenset(),
}


@dataclass(slots=True)
class TaskStateMachine:
    current: TaskState = TaskState.PREFLIGHT
    _history: list[TaskState] = field(default_factory=lambda: [TaskState.PREFLIGHT])

    @property
    def history(self) -> tuple[TaskState, ...]:
        return tuple(self._history)

    def transition(self, target: TaskState) -> None:
        if target not in ALLOWED_TRANSITIONS[self.current]:
            raise InvalidTransition(f"Cannot transition from {self.current.value} to {target.value}.")
        self.current = target
        self._history.append(target)
