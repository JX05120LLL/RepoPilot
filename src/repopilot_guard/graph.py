"""第四阶段：可恢复、只读且受限的 LangGraph Coding Workflow。

阶段四 Step 2：本文件已拆分为 `repopilot_guard/graph_impl/` 子包。
本模块保留为兼容 shim，统一重导出全部既有名字，既有
`from repopilot_guard.graph import ...` 调用方无需改动；
新代码建议直接从 `repopilot_guard.graph_impl` 的对应子模块导入。

子包结构：
- states.py            核心状态与数据模型（GraphState/ChangePlan/ToolCall 等）
- model_invocation.py  模型调用与协作式取消
- preflight.py         PREFLIGHT 检查器
- research_model.py    ResearchModel 协议、契约错误与 OpenAI 实现
- context_services.py  RAG 上下文服务与 Noop 实现
- live_graph.py        create_live_graph 真实依赖组装
- research_tools.py    研究工具执行器与 checkpoint 存储
- factory.py           CodingGraphFactory / GraphRunner（LangGraph 实现）
- helpers.py           状态投影与审计辅助函数
"""

from __future__ import annotations

import time as time  # noqa: F401 —— 测试以 patch("repopilot_guard.graph.time.sleep") 引用

from repopilot_guard.graph_impl.context_services import (
    ContextService,
    LiveContextService,
    NoopContextService,
    NoopProjectMemoryWriter,
    ProjectMemoryWriter,
)
from repopilot_guard.graph_impl.factory import (
    CodingGraphFactory,
    GraphRunResult,
    GraphRunner,
    PATCH_APPLICATION_REPAIR_ATTEMPTS,
    PATCH_CONTRACT_ATTEMPTS,
    PLAN_CONTRACT_ATTEMPTS,
)
from repopilot_guard.graph_impl.helpers import (
    _AUDIT_SECRET_PATTERN,
    _RESEARCH_TOOL_DESCRIPTIONS,
    _allows_non_git_local_research,
    _block_if_model_budget_exceeded,
    _block_if_model_budget_reached,
    _blocked,
    _budget_from_state,
    _budget_usage,
    _context_reference,
    _deduplicate_subagent_references,
    _has_pytest_project,
    _mcp_bindings_from_state,
    _model_budget_decision,
    _model_usage_event,
    _normalize_plan_candidates,
    _observed_candidate_paths,
    _patch_selection_digest,
    _permission_from_state,
    _permission_snapshot_from_state,
    _plan_evidence_catalog,
    _plan_evidence_issues,
    _plan_matches_verification_contract,
    _project_id,
    _proposal_for_selected_paths,
    _retrieval_message,
    _risk_preview_digest,
    _safe_arguments,
    _selected_patch_paths,
    _shell_command_contains_secret,
    _tool_summary,
    _usage_integer,
    _validation_issue_summary,
    _workspace_from_state,
    research_capability_registry,
)
from repopilot_guard.graph_impl.live_graph import create_live_graph
from repopilot_guard.graph_impl.model_invocation import (
    ModelInvocationCancelled,
    _await_cancellable_model_response,
    _invoke_model_request,
    _model_cancellation_scope,
    _raise_if_model_cancelled,
)
from repopilot_guard.graph_impl.preflight import (
    GraphPreflightChecker,
    PhaseOnePreflightChecker,
    PhaseOnePreflightResult,
)
from repopilot_guard.graph_impl.research_model import (
    MODEL_OPERATION_ATTEMPTS,
    MODEL_RETRY_BASE_DELAY_SECONDS,
    SHELL_CONTRACT_ATTEMPTS,
    NoopResearchModel,
    OpenAIResearchModel,
    PatchContractError,
    PatchGenerationResult,
    PlanContractError,
    PlanGenerationResult,
    ResearchModel,
    ShellCommandContractError,
    ShellGenerationResult,
)
from repopilot_guard.graph_impl.research_tools import (
    MAX_EXECUTION_RESEARCH_ROUNDS,
    MAX_RESEARCH_ROUNDS,
    MAX_RESEARCH_TOOL_OUTPUT_CHARS,
    MAX_TOOL_CALLS,
    MAX_VERIFICATION_OBSERVATION_ROUNDS,
    ResearchToolExecutor,
    SqliteCheckpointStore,
)
from repopilot_guard.graph_impl.states import (
    ChangePlan,
    EvidenceReference,
    GraphState,
    GraphWorkspaceContext,
    ModelUsage,
    ResearchDecision,
    ToolCall,
)

__all__ = [
    "ChangePlan",
    "CodingGraphFactory",
    "ContextService",
    "EvidenceReference",
    "GraphPreflightChecker",
    "GraphRunResult",
    "GraphRunner",
    "GraphState",
    "GraphWorkspaceContext",
    "LiveContextService",
    "MAX_EXECUTION_RESEARCH_ROUNDS",
    "MAX_RESEARCH_ROUNDS",
    "MAX_RESEARCH_TOOL_OUTPUT_CHARS",
    "MAX_TOOL_CALLS",
    "MAX_VERIFICATION_OBSERVATION_ROUNDS",
    "MODEL_OPERATION_ATTEMPTS",
    "MODEL_RETRY_BASE_DELAY_SECONDS",
    "ModelInvocationCancelled",
    "ModelUsage",
    "NoopContextService",
    "NoopProjectMemoryWriter",
    "NoopResearchModel",
    "OpenAIResearchModel",
    "PATCH_APPLICATION_REPAIR_ATTEMPTS",
    "PATCH_CONTRACT_ATTEMPTS",
    "PLAN_CONTRACT_ATTEMPTS",
    "PatchContractError",
    "PatchGenerationResult",
    "PhaseOnePreflightChecker",
    "PhaseOnePreflightResult",
    "PlanContractError",
    "PlanGenerationResult",
    "ProjectMemoryWriter",
    "ResearchDecision",
    "ResearchModel",
    "ResearchToolExecutor",
    "SHELL_CONTRACT_ATTEMPTS",
    "ShellCommandContractError",
    "ShellGenerationResult",
    "SqliteCheckpointStore",
    "ToolCall",
    "_AUDIT_SECRET_PATTERN",
    "_RESEARCH_TOOL_DESCRIPTIONS",
    "_allows_non_git_local_research",
    "_await_cancellable_model_response",
    "_block_if_model_budget_exceeded",
    "_block_if_model_budget_reached",
    "_blocked",
    "_budget_from_state",
    "_budget_usage",
    "_context_reference",
    "_deduplicate_subagent_references",
    "_has_pytest_project",
    "_invoke_model_request",
    "_mcp_bindings_from_state",
    "_model_budget_decision",
    "_model_cancellation_scope",
    "_model_usage_event",
    "_normalize_plan_candidates",
    "_observed_candidate_paths",
    "_patch_selection_digest",
    "_permission_from_state",
    "_permission_snapshot_from_state",
    "_plan_evidence_catalog",
    "_plan_evidence_issues",
    "_plan_matches_verification_contract",
    "_project_id",
    "_proposal_for_selected_paths",
    "_raise_if_model_cancelled",
    "_retrieval_message",
    "_risk_preview_digest",
    "_safe_arguments",
    "_selected_patch_paths",
    "_shell_command_contains_secret",
    "_tool_summary",
    "_usage_integer",
    "_validation_issue_summary",
    "_workspace_from_state",
    "create_live_graph",
    "research_capability_registry",
]
