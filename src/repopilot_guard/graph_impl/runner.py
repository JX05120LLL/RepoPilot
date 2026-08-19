"""图执行结果与运行器（阶段四 Step 2 自 factory.py 拆出，DR-025）。

GraphRunner 持有编译好的 LangGraph 图与 checkpoint，负责单次任务的
执行 / 恢复 / 读取快照；GraphRunResult 是任务的终态投影。它们是
AgentHandle 契约（见 agent_handle.py / graph_bridge.py）背后的引擎侧实现。
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Thread
from typing import Any, Callable
from uuid import uuid4

from langgraph.types import Command

from repopilot_guard.cancellation import DEFAULT_CANCELLATION_REGISTRY, TaskCancellationRegistry
from repopilot_guard.models import TaskBudget, TaskRequest
from repopilot_guard.permissions import PermissionGrant, PermissionSnapshot

from .helpers import _permission_snapshot_from_state, _selected_patch_paths
from .states import GraphState


@dataclass(frozen=True)
class GraphRunResult:
    thread_id: str
    task_id: str
    status: str
    pending_approval: bool
    verdict: str | None
    state: GraphState
    interrupts: tuple[dict[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        return {"thread_id": self.thread_id, "task_id": self.task_id, "status": self.status, "pending_approval": self.pending_approval, "verdict": self.verdict, "interrupts": list(self.interrupts), "state": self.state}


class GraphRunner:
    def __init__(
        self,
        graph: Any,
        cancellation_registry: TaskCancellationRegistry | None = None,
        default_budget: TaskBudget | None = None,
    ) -> None:
        self._graph = graph
        self._cancellations = cancellation_registry or DEFAULT_CANCELLATION_REGISTRY
        self._default_budget = default_budget or TaskBudget()
        release = getattr(graph, "_repopilot_mcp_release", None)
        self._mcp_release: Callable[[str | None], bool] | None = release if callable(release) else None

    def run(self, request: TaskRequest, thread_id: str | None = None, permission: PermissionGrant | None = None) -> GraphRunResult:
        selected_thread_id = thread_id or str(uuid4())
        self._cancellations.begin(selected_thread_id)
        try:
            grant = permission or PermissionGrant.safe()
            permission_snapshot = PermissionSnapshot.create(
                request.task_id,
                grant,
                request.workspace_selection.mode.value,
                request.approved_mcp_tools,
                request.operation.value,
                request.approved_capabilities,
                request.approved_mcp_sources,
            )
            budget = request.budget.restricted_by(self._default_budget)
            initial_state: GraphState = {
                "thread_id": selected_thread_id,
                "task_id": request.task_id,
                "status": "INTAKE",
                "verdict": None,
                "messages": [
                    *(
                        [{"role": "user", "content": request.conversation_context}]
                        if request.conversation_context
                        else []
                    ),
                    {"role": "user", "content": request.description},
                ],
                "tool_events": [{"type": "TASK_BUDGET_SNAPSHOT", **budget.to_dict()}],
                "pending_approval": False,
                "repository": str(request.repository),
                "output_root": str(request.output_root),
                "task_description": request.description,
                "task_operation": request.operation.value,
                "verification_contract": request.verification_contract.to_dict() if request.verification_contract else None,
                "budget_snapshot": budget.to_dict(),
                "approved_mcp_tools": list(request.approved_mcp_tools),
                "approved_mcp_sources": list(request.approved_mcp_sources),
                "approved_capabilities": list(request.approved_capabilities),
                "shell_runtime_enabled": None,
                "attached_document_ids": list(request.attached_document_ids),
                "attached_documents": [],
                "project_id": request.project_id,
                "conversation_id": request.conversation_id,
                "conversation_context": request.conversation_context,
                "capability_profile": request.capability_profile,
                "permission_mode": grant.mode.value,
                "permission_confirmation": grant.confirmation,
                "permission_snapshot": permission_snapshot.to_dict(),
                "workspace_mode": request.workspace_selection.mode.value,
                "start_ref": request.workspace_selection.start_ref,
                "include_uncommitted_changes": request.workspace_selection.include_uncommitted_changes,
                "workspace_path": None,
                "base_commit": None,
                "workspace_dirty_entries": [],
                "context_references": [],
                "context_snapshot": None,
                "subagent_findings": [],
                "mcp_bindings": [],
                "candidate_files": [],
                "research_rounds": 0,
                "tool_call_count": 0,
                "tool_output_chars": 0,
                "pending_tool_calls": [],
                "execution_research_rounds": 0,
                "execution_pending_tool_calls": [],
                "verification_observation_rounds": 0,
                "verification_pending_tool_calls": [],
                "plan": None,
                "pending_approval_action": None,
                "approval_feedback": None,
                "plan_revision": 0,
                "error_summary": None,
                "patch_proposal": None,
                "patch_preview": None,
                "selected_patch_paths": [],
                "patch_selection_sha256": None,
                "selected_patch_preview_sha256": None,
                "shell_proposal": None,
                "shell_previews": [],
                "shell_results": [],
                "risk_approval_sha256": None,
                "risk_approved": False,
                "patch_result": None,
                "verification_result": None,
                "git_diff": None,
            }
            self._graph.invoke(initial_state, self._config(selected_thread_id))
            return self._snapshot(selected_thread_id)
        finally:
            self._cancellations.release(selected_thread_id)

    def resume(
        self,
        thread_id: str,
        approved: bool | None = None,
        *,
        decision: str | None = None,
        comment: str | None = None,
        selected_patch_paths: list[str] | tuple[str, ...] | None = None,
    ) -> GraphRunResult:
        current = self._snapshot(thread_id)
        if not current.pending_approval:
            raise ValueError("NO_PENDING_APPROVAL")
        snapshot = _permission_snapshot_from_state(current.state)
        if (
            snapshot.task_id != current.task_id
            or snapshot.workspace_mode != current.state["workspace_mode"]
            or snapshot.task_operation != current.state["task_operation"]
            or snapshot.grant.mode.value != current.state["permission_mode"]
            or snapshot.grant.confirmation != current.state.get("permission_confirmation")
            or snapshot.approved_mcp_tools != tuple(current.state.get("approved_mcp_tools", []))
            or snapshot.approved_mcp_sources != tuple(current.state.get("approved_mcp_sources", []))
            or snapshot.approved_capabilities != tuple(current.state.get("approved_capabilities", []))
        ):
            raise ValueError("PERMISSION_SNAPSHOT_MISMATCH")
        resolved = decision or ("approve" if approved is True else "reject")
        if resolved not in {"approve", "revise", "reject"}:
            raise ValueError("INVALID_APPROVAL_DECISION")
        if resolved == "revise" and (not comment or not comment.strip()):
            raise ValueError("PLAN_REVISION_FEEDBACK_REQUIRED")
        selected_paths: tuple[str, ...] | None = None
        if selected_patch_paths is not None:
            if resolved != "approve":
                raise ValueError("PATCH_SELECTION_REQUIRES_APPROVAL")
            execution_interrupt = next(
                (item for item in current.interrupts if item.get("type") == "EXECUTION_APPROVAL_REQUIRED"),
                None,
            )
            if not isinstance(execution_interrupt, dict):
                raise ValueError("PATCH_SELECTION_NOT_ALLOWED")
            preview = execution_interrupt.get("patch_preview")
            if not isinstance(preview, dict):
                raise ValueError("PATCH_PREVIEW_MISSING")
            selected_paths = _selected_patch_paths(preview, selected_patch_paths)
        self._cancellations.begin(thread_id)
        try:
            self._graph.invoke(
                Command(
                    resume={
                        "approved": resolved == "approve",
                        "decision": resolved,
                        "comment": comment,
                        "selected_patch_paths": list(selected_paths) if selected_paths is not None else None,
                    }
                ),
                self._config(thread_id),
            )
            return self._snapshot(thread_id)
        finally:
            self._cancellations.release(thread_id)

    def request_cancellation(self, thread_id: str, reason: str | None = None) -> None:
        """取消 API 调用此入口唤醒正在运行的图；持久状态仍由 TaskStore 负责。"""

        self._cancellations.request(thread_id, reason)
        if self._mcp_release is not None:
            # 取消接口不能等待第三方 MCP 的收尾；释放线程会按 Runtime Actor 的顺序关闭已接受调用。
            Thread(target=self._mcp_release, args=(thread_id,), name="repopilot-mcp-release", daemon=True).start()

    def get(self, thread_id: str) -> GraphRunResult:
        """读取已持久化任务，不触发模型、工具或工作区操作。"""
        return self._snapshot(thread_id)

    @staticmethod
    def _config(thread_id: str) -> dict[str, dict[str, str]]:
        return {"configurable": {"thread_id": thread_id}}

    def _snapshot(self, thread_id: str) -> GraphRunResult:
        snapshot = self._graph.get_state(self._config(thread_id))
        state = dict(snapshot.values)
        interrupts: list[dict[str, object]] = []
        for task in snapshot.tasks:
            for item in task.interrupts:
                value = getattr(item, "value", item)
                if isinstance(value, dict):
                    interrupts.append(value)
        return GraphRunResult(thread_id=state["thread_id"], task_id=state["task_id"], status=state["status"], pending_approval=state["pending_approval"], verdict=state.get("verdict"), state=state, interrupts=tuple(interrupts))
