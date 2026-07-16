"""编排当前只读的干跑任务生命周期。"""

from __future__ import annotations

from repopilot_guard.evidence import EvidenceStore
from repopilot_guard.models import TaskRequest, TaskResult, TaskState, TaskVerdict
from repopilot_guard.preflight import PreflightInspector
from repopilot_guard.state_machine import TaskStateMachine


class TaskCoordinator:
    """在尚未授予写入和执行权限时，编排预检与计划阶段。"""

    def __init__(self, preflight_inspector: PreflightInspector | None = None) -> None:
        self.preflight_inspector = preflight_inspector or PreflightInspector()

    def run_dry_run(self, request: TaskRequest) -> TaskResult:
        evidence = EvidenceStore(request.output_root, request.task_id)
        state_machine = TaskStateMachine()
        evidence.record(
            "task_created",
            {
                "task_id": request.task_id,
                "repository": str(request.repository),
                "max_steps": request.max_steps,
            },
        )

        preflight = self.preflight_inspector.inspect(request.repository)
        evidence.record("preflight_completed", preflight.to_dict())

        if not preflight.ready:
            state_machine.transition(TaskState.BLOCKED)
            evidence.record("task_blocked", {"errors": list(preflight.errors)})
            result = TaskResult(
                task_id=request.task_id,
                repository=request.repository,
                verdict=TaskVerdict.BLOCKED,
                final_state=state_machine.current,
                state_history=state_machine.history,
                preflight=preflight,
                report_path=evidence.report_path,
                events_path=evidence.events_path,
                message="Preflight failed; no workspace, patch or test command was executed.",
            )
        else:
            for state in (TaskState.UNDERSTAND, TaskState.LOCATE, TaskState.PLAN, TaskState.REVIEW, TaskState.REPORT):
                state_machine.transition(state)
                evidence.record("state_changed", {"state": state.value})

            result = TaskResult(
                task_id=request.task_id,
                repository=request.repository,
                verdict=TaskVerdict.UNVERIFIED,
                final_state=state_machine.current,
                state_history=state_machine.history,
                preflight=preflight,
                report_path=evidence.report_path,
                events_path=evidence.events_path,
                message="Dry-run completed. No patch or test was executed, so the task remains unverified.",
            )

        evidence.write_report(result)
        evidence.record("task_completed", result.to_dict())
        return result
