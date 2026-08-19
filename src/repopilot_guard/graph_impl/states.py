"""阶段四 Step 2：graph.py 拆分出的子模块（由 graph.py 兼容 shim 统一重导出）。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from pydantic import BaseModel, Field, model_validator

from repopilot_guard.models import WorkspaceMode
from repopilot_guard.policy import (
    GradleRecipeName,
    MavenRecipeName,
    NodeRecipeName,
    NoVerificationRecipeName,
    PytestRecipeName,
)

class EvidenceReference(BaseModel):
    """计划引用的代码、文档或工具证据。"""

    source_type: str
    path: str
    line_start: int | None = None
    line_end: int | None = None
    note: str


class ChangePlan(BaseModel):
    """阶段四生成的结构化计划，尚不代表代码已修改。"""

    summary: str = Field(min_length=1)
    evidence: list[EvidenceReference] = Field(default_factory=list)
    candidate_files: list[str] = Field(default_factory=list)
    # 由服务端从模型原始候选中投影，模型不能借此字段扩大补丁范围。
    unverified_candidate_files: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    verification: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    verification_recipe: MavenRecipeName | GradleRecipeName | PytestRecipeName | NodeRecipeName | NoVerificationRecipeName = MavenRecipeName.TEST
    target_test_class: str | None = None

    @model_validator(mode="after")
    def validate_verification_target(self) -> "ChangePlan":
        if self.verification_recipe in {MavenRecipeName.TARGETED_TEST, GradleRecipeName.TARGETED_TEST, PytestRecipeName.TARGETED_TEST} and not self.target_test_class:
            raise ValueError("targeted_test 必须提供 target_test_class")
        if self.verification_recipe not in {MavenRecipeName.TARGETED_TEST, GradleRecipeName.TARGETED_TEST, PytestRecipeName.TARGETED_TEST} and self.target_test_class is not None:
            raise ValueError("compile/test 不允许提供 target_test_class")
        return self


@dataclass(frozen=True, slots=True)
class GraphWorkspaceContext:
    """图内使用的已准备工作区快照。"""

    workspace_path: Path
    base_commit: str
    mode: WorkspaceMode


@dataclass(frozen=True, slots=True)
class ToolCall:
    name: str
    arguments: dict[str, object]


@dataclass(frozen=True, slots=True)
class ModelUsage:
    """只接受供应商返回的用量；0 不代表免费，而是供应商未报告。"""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    reported: bool = False
    estimated_cost: float | None = None
    currency: str | None = None

    def add(self, other: "ModelUsage") -> "ModelUsage":
        if not self.reported and not other.reported:
            return ModelUsage()
        if not self.reported:
            return other
        if not other.reported:
            return self
        if self.estimated_cost is None or other.estimated_cost is None:
            cost = None
            currency = None
        else:
            cost = round(self.estimated_cost + other.estimated_cost, 8)
            currency = self.currency if self.currency == other.currency else None
        return ModelUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            reported=True,
            estimated_cost=cost,
            currency=currency,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "reported": self.reported,
            "estimated_cost": self.estimated_cost,
            "currency": self.currency,
        }


@dataclass(frozen=True, slots=True)
class ResearchDecision:
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    usage: ModelUsage = ModelUsage()


class GraphState(TypedDict, total=False):
    """持久化在 SQLite checkpoint 的任务状态；模型不能直接写入此状态。"""

    thread_id: str
    task_id: str
    status: str
    verdict: str | None
    messages: list[dict[str, str]]
    tool_events: list[dict[str, object]]
    pending_approval: bool
    repository: str
    output_root: str
    task_description: str
    task_operation: str
    verification_contract: dict[str, object] | None
    budget_snapshot: dict[str, object]
    approved_mcp_tools: list[str]
    approved_mcp_sources: list[str]
    approved_capabilities: list[str]
    shell_runtime_enabled: bool | None
    attached_document_ids: list[str]
    attached_documents: list[dict[str, str]]
    project_id: str | None
    conversation_id: str | None
    conversation_context: str
    capability_profile: dict[str, object] | None
    permission_mode: str
    permission_confirmation: str | None
    permission_snapshot: dict[str, object]
    workspace_mode: str
    start_ref: str
    include_uncommitted_changes: bool
    workspace_path: str | None
    base_commit: str | None
    workspace_dirty_entries: list[str]
    context_references: list[dict[str, object]]
    context_snapshot: dict[str, object] | None
    subagent_findings: list[dict[str, object]]
    mcp_bindings: list[dict[str, object]]
    candidate_files: list[str]
    research_rounds: int
    tool_call_count: int
    tool_output_chars: int
    pending_tool_calls: list[dict[str, object]]
    execution_research_rounds: int
    execution_pending_tool_calls: list[dict[str, object]]
    verification_observation_rounds: int
    verification_pending_tool_calls: list[dict[str, object]]
    plan: dict[str, object] | None
    pending_approval_action: str | None
    approval_feedback: str | None
    plan_revision: int
    error_summary: str | None
    patch_proposal: dict[str, object] | None
    patch_preview: dict[str, object] | None
    selected_patch_paths: list[str]
    patch_selection_sha256: str | None
    selected_patch_preview_sha256: str | None
    shell_proposal: dict[str, object] | None
    shell_previews: list[dict[str, object]]
    shell_results: list[dict[str, object]]
    risk_approval_sha256: str | None
    risk_approved: bool
    patch_result: dict[str, object] | None
    verification_result: dict[str, object] | None
    git_diff: str | None
