"""编排当前只读的干跑任务生命周期。"""

from __future__ import annotations

from dataclasses import replace

from repopilot_guard.evidence import EvidenceStore
from repopilot_guard.models import TaskRequest, TaskResult, TaskState, TaskVerdict
from repopilot_guard.preflight import PreflightInspector
from repopilot_guard.state_machine import TaskStateMachine
from repopilot_guard.graph import GraphRunResult, GraphRunner
from repopilot_guard.permissions import PermissionGrant
from repopilot_guard.project_registry import ProjectRegistry
from repopilot_guard.workspace import WorkspaceManager, WorkspacePreparationResult


class TaskCoordinator:
    """在尚未授予写入和执行权限时，编排预检与计划阶段。"""

    def __init__(
        self,
        preflight_inspector: PreflightInspector | None = None,
        workspace_manager: WorkspaceManager | None = None,
        project_registry: ProjectRegistry | None = None,
    ) -> None:
        self.preflight_inspector = preflight_inspector or PreflightInspector()
        self.workspace_manager = workspace_manager or WorkspaceManager()
        self.project_registry = project_registry

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

    def run_graph(self, request: TaskRequest, graph_runner: GraphRunner, thread_id: str | None = None) -> GraphRunResult:
        """委托最小 LangGraph；PolicyGuard 仍由未来执行层独立调用。"""

        return graph_runner.run(request, thread_id)

    def prepare_workspace(self, request: TaskRequest, permission: PermissionGrant) -> WorkspacePreparationResult:
        """为后续代码工具准备隔离环境，并写入任务级审计证据。"""

        evidence = EvidenceStore(request.output_root, request.task_id)
        evidence.record(
            "workspace_preparation_requested",
            {
                "task_id": request.task_id,
                "repository": str(request.repository),
                "permission": permission.to_dict(),
            },
        )
        result = self.workspace_manager.prepare(request, permission)
        if result.status == "READY" and result.workspace_path and result.base_commit and self.project_registry:
            self.project_registry.record_workspace(
                task_id=request.task_id,
                project_id=request.project_id,
                mode=result.mode.value,
                workspace_path=result.workspace_path,
                base_commit=result.base_commit,
                created_at=result.created_at,
            )
        event_type = "workspace_prepared" if result.status == "READY" else "workspace_blocked"
        evidence.record(event_type, result.to_dict())
        return replace(result, evidence_events_path=evidence.events_path)
