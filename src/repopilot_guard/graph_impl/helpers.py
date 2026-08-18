"""阶段四 Step 2：graph.py 拆分出的子模块（由 graph.py 兼容 shim 统一重导出）。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from repopilot_guard.capabilities import (
    CapabilityDescriptor,
    CapabilityKind,
    CapabilityRegistry,
    CapabilityRisk,
    CapabilityScope,
)
from repopilot_guard.context import RetrievalResult
from repopilot_guard.execution import PatchProposal
from repopilot_guard.mcp_agent import McpToolBinding
from repopilot_guard.models import TaskBudget, TaskOperation, VerificationContract, WorkspaceMode
from repopilot_guard.permissions import PermissionGrant, PermissionMode, PermissionSnapshot
from repopilot_guard.shell_runtime import ShellCommandRequest

from .preflight import PhaseOnePreflightResult
from .states import ChangePlan, GraphState, GraphWorkspaceContext, ModelUsage

_AUDIT_SECRET_PATTERN = re.compile(r"(?i)(api[_-]?key|authorization|password|secret|token)(\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+")

_RESEARCH_TOOL_DESCRIPTIONS = {
    "list_files": "列出允许范围内的仓库文件。",
    "search_code": "在允许范围内按字面量搜索代码。",
    "find_symbol": "按 Java 类型或方法声明名定位受保护工作区内的代码。",
    "read_file": "读取一个允许的 UTF-8 文本文件。",
    "inspect_build": "读取受支持的构建描述，不执行构建命令。",
    "retrieve_context": "按当前项目和提交检索已索引上下文。",
}

def _permission_from_state(state: GraphState) -> PermissionGrant:
    return _permission_snapshot_from_state(state).grant


def _budget_from_state(state: GraphState) -> TaskBudget:
    return TaskBudget.from_dict(state.get("budget_snapshot"))


def _budget_usage(events: object) -> ModelUsage:
    total = ModelUsage()
    for item in events if isinstance(events, list) else ():
        if not isinstance(item, dict) or item.get("type") != "MODEL_USAGE":
            continue
        reported = item.get("reported") is True
        total = total.add(
            ModelUsage(
                input_tokens=_usage_integer(item, "input_tokens"),
                output_tokens=_usage_integer(item, "output_tokens"),
                total_tokens=_usage_integer(item, "total_tokens"),
                reported=reported,
                estimated_cost=float(item["estimated_cost"]) if isinstance(item.get("estimated_cost"), (int, float)) else None,
                currency=item["currency"] if isinstance(item.get("currency"), str) else None,
            )
        )
    return total


def _model_budget_decision(state: GraphState, *, after_call: bool) -> tuple[str, str, dict[str, object]] | None:
    """预算前用“达到上限”阻止下一次调用，调用后用“超过上限”阻止继续流转。"""

    budget = _budget_from_state(state)
    if not budget.configured:
        return None
    events = state.get("tool_events")
    usage = _budget_usage(events)
    has_model_usage = any(isinstance(item, dict) and item.get("type") == "MODEL_USAGE" for item in events) if isinstance(events, list) else False
    details = {"type": "MODEL_BUDGET_STATUS", **budget.to_dict(), **usage.to_dict()}
    if budget.max_total_tokens is not None:
        if has_model_usage and not usage.reported:
            return "MODEL_USAGE_UNAVAILABLE", "已配置 Token 预算，但模型供应商未回传用量；已停止后续模型调用。", details
        exceeded = usage.total_tokens > budget.max_total_tokens if after_call else usage.total_tokens >= budget.max_total_tokens
        if exceeded:
            code = "MODEL_TOKEN_BUDGET_EXCEEDED" if after_call else "MODEL_TOKEN_BUDGET_REACHED"
            return code, "模型 Token 预算已达到或超过上限；已停止后续模型调用。", details
    if budget.max_estimated_cost is not None:
        if not has_model_usage:
            return None
        if has_model_usage and (usage.estimated_cost is None or usage.currency != budget.currency):
            return "MODEL_COST_UNAVAILABLE", "已配置成本预算，但无法用供应商用量和当前计价配置可靠估算成本；已停止后续模型调用。", details
        exceeded = usage.estimated_cost > budget.max_estimated_cost if after_call else usage.estimated_cost >= budget.max_estimated_cost
        if exceeded:
            code = "MODEL_COST_BUDGET_EXCEEDED" if after_call else "MODEL_COST_BUDGET_REACHED"
            return code, "模型成本预算已达到或超过上限；已停止后续模型调用。", details
    return None


def _block_if_model_budget_reached(state: GraphState) -> GraphState | None:
    decision = _model_budget_decision(state, after_call=False)
    if decision is None:
        return None
    code, message, event = decision
    return _blocked(state, code, message, event)


def _block_if_model_budget_exceeded(state: GraphState) -> GraphState | None:
    decision = _model_budget_decision(state, after_call=True)
    if decision is None:
        return None
    code, message, event = decision
    return _blocked(state, code, message, event)


def _permission_snapshot_from_state(state: GraphState) -> PermissionSnapshot:
    raw_snapshot = state.get("permission_snapshot")
    if isinstance(raw_snapshot, dict):
        return PermissionSnapshot.from_dict(raw_snapshot)
    return PermissionSnapshot.create(
        str(state["task_id"]),
        PermissionGrant(PermissionMode(state["permission_mode"]), state.get("permission_confirmation")),
        str(state["workspace_mode"]),
        tuple(state.get("approved_mcp_tools", [])),
        task_operation=str(state.get("task_operation", TaskOperation.CHANGE.value)),
        approved_capabilities=tuple(state.get("approved_capabilities", [])),
        approved_mcp_sources=tuple(state.get("approved_mcp_sources", [])),
    )


def _workspace_from_state(state: GraphState) -> GraphWorkspaceContext:
    return GraphWorkspaceContext(Path(str(state["workspace_path"])), str(state["base_commit"]), WorkspaceMode(state["workspace_mode"]))


def _project_id(state: GraphState) -> str:
    return str(state.get("project_id") or f"adhoc-{state['repository']}")


def _allows_non_git_local_research(state: GraphState, result: PhaseOnePreflightResult) -> bool:
    """完全本机控制可进入非 Git 目录，使用文件快照而非伪造 Git 基线。"""
    repository_check = next((check for check in result.checks if check.component == "repository"), None)
    return bool(
        repository_check
        and state.get("workspace_mode") == WorkspaceMode.LOCAL.value
        and state.get("permission_mode") == PermissionMode.FULL.value
        and repository_check.code == "REPOSITORY_PREFLIGHT_FAILED"
        and "Repository is not a Git working tree." in repository_check.missing_fields
    )


def _blocked(state: GraphState, code: str, message: str, event: dict[str, object] | None = None) -> GraphState:
    events = [*state["tool_events"], *( [event] if event else [] ), {"type": "GRAPH_BLOCKED", "code": code, "message": message}]
    return {"status": "BLOCKED", "verdict": "BLOCKED", "pending_approval": False, "error_summary": message, "tool_events": events}


def _plan_matches_verification_contract(plan: ChangePlan, raw_contract: object) -> bool:
    if raw_contract is None:
        return True
    try:
        contract = VerificationContract.from_dict(raw_contract)
    except ValueError:
        return False
    return plan.verification_recipe.value == contract.recipe and plan.target_test_class == contract.target_test_class


def _plan_evidence_catalog(state: GraphState) -> list[dict[str, object]]:
    """仅投影已检索或成功读取的来源定位信息，不把代码正文或工具输出再写入提示。"""

    catalog: list[dict[str, object]] = []
    for item in state.get("context_references", []):
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        source_type = item.get("source_type")
        if not isinstance(path, str) or not path or not isinstance(source_type, str) or not source_type:
            continue
        catalog.append(
            {
                "source_type": source_type,
                "path": path,
                "line_start": item.get("line_start"),
                "line_end": item.get("line_end"),
            }
        )
    for event in state.get("tool_events", []):
        if not isinstance(event, dict) or event.get("type") != "TOOL_CALL" or event.get("status") != "READY":
            continue
        arguments = event.get("arguments")
        path = arguments.get("path") if isinstance(arguments, dict) else None
        if isinstance(path, str) and path and path != ".":
            catalog.append({"source_type": "tool", "path": path, "line_start": None, "line_end": None})
    return catalog


def _plan_evidence_issues(plan: ChangePlan, state: GraphState) -> list[dict[str, str]]:
    """计划只能引用运行时已观察到的来源，避免模型把猜测路径伪装为工程事实。"""

    observed = _plan_evidence_catalog(state)
    issues: list[dict[str, str]] = []
    for index, reference in enumerate(plan.evidence):
        matches = [item for item in observed if item["path"] == reference.path]
        if not matches:
            issues.append({"field": f"evidence.{index}.path", "rule": "source_not_observed"})
            continue
        typed_matches = [item for item in matches if item["source_type"] == reference.source_type]
        if not typed_matches:
            issues.append({"field": f"evidence.{index}.source_type", "rule": "source_type_not_observed"})
            continue
        if reference.line_start is None and reference.line_end is None:
            continue
        ranged_matches = [
            item
            for item in typed_matches
            if isinstance(item.get("line_start"), int)
            and isinstance(item.get("line_end"), int)
            and (reference.line_start is None or item["line_start"] <= reference.line_start)
            and (reference.line_end is None or reference.line_end <= item["line_end"])
        ]
        if not ranged_matches:
            issues.append({"field": f"evidence.{index}.line_start", "rule": "line_range_not_observed"})
    return issues


def _observed_candidate_paths(state: GraphState) -> set[str]:
    """仅将当前工作区内、已被 RAG 或 read_file 实际观察的文件送入补丁候选范围。"""

    allowed_source_types = {"code", "build_config", "configuration", "repository_document"}
    paths: set[str] = set()
    for item in state.get("context_references", []):
        if not isinstance(item, dict) or item.get("source_type") not in allowed_source_types:
            continue
        path = item.get("path")
        if isinstance(path, str) and path and not Path(path).is_absolute() and path != ".":
            paths.add(path)
    for event in state.get("tool_events", []):
        if not isinstance(event, dict) or event.get("type") != "TOOL_CALL" or event.get("status") != "READY" or event.get("name") != "read_file":
            continue
        arguments = event.get("arguments")
        path = arguments.get("path") if isinstance(arguments, dict) else None
        if isinstance(path, str) and path and not Path(path).is_absolute() and path != ".":
            paths.add(path)
    return paths


def _normalize_plan_candidates(plan: ChangePlan, state: GraphState) -> ChangePlan:
    """模型声明的候选文件必须是已观察路径；其余路径只作为人工补充研究提示保留。"""

    observed = _observed_candidate_paths(state)
    requested = {path for path in plan.candidate_files if isinstance(path, str) and path}
    verified = sorted(requested.intersection(observed))
    unverified = sorted(requested.difference(observed))
    return plan.model_copy(update={"candidate_files": verified, "unverified_candidate_files": unverified})


def _has_pytest_project(workspace_root: Path) -> bool:
    """仅以受控项目描述文件冻结 pytest 验证契约，不扫描用户目录或任意依赖。"""

    return any((workspace_root / name).is_file() for name in ("pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg"))


def _context_reference(item: Any) -> dict[str, object]:
    return {"source_type": item.source_type, "path": item.path, "line_start": item.line_start, "line_end": item.line_end, "note": "RAG 检索结果"}


def _deduplicate_subagent_references(items: list[dict[str, object]]) -> list[dict[str, object]]:
    """父 Agent 只保留有限、可定位的子 Agent 证据，防止并行结果挤占上下文。"""

    selected: list[dict[str, object]] = []
    seen: set[tuple[object, object, object, object]] = set()
    for item in items:
        path = item.get("path")
        if not isinstance(path, str) or not path:
            continue
        key = (item.get("source_type"), path, item.get("line_start"), item.get("line_end"))
        if key in seen:
            continue
        seen.add(key)
        selected.append(item)
        if len(selected) >= 64:
            break
    return selected


def research_capability_registry() -> CapabilityRegistry:
    """返回研究阶段固定的只读能力目录，供 Broker、执行器和本机 API 共享。"""
    return CapabilityRegistry(
        CapabilityDescriptor(
            capability_id=name,
            name=name,
            description=description,
            kind=CapabilityKind.BUILTIN_TOOL,
            scope=CapabilityScope.BUNDLED,
            source="repopilot:research",
            risks=frozenset({CapabilityRisk.READ}),
        )
        for name, description in _RESEARCH_TOOL_DESCRIPTIONS.items()
    )


def _mcp_bindings_from_state(state: GraphState) -> tuple[McpToolBinding, ...]:
    raw_bindings = state.get("mcp_bindings", [])
    if not isinstance(raw_bindings, list):
        raise ValueError("MCP_BINDING_SNAPSHOT_INVALID")
    bindings = tuple(McpToolBinding.from_dict(item) for item in raw_bindings)
    identifiers = [binding.capability_id for binding in bindings]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("MCP_BINDING_SNAPSHOT_INVALID")
    return bindings


def _retrieval_message(result: RetrievalResult) -> str:
    if not result.contexts:
        return "未检索到向量上下文；可继续使用受控仓库工具研究。"
    references = [f"{item.path}:{item.line_start}-{item.line_end}" for item in result.contexts]
    return "已检索上下文：" + ", ".join(references)


def _safe_arguments(arguments: dict[str, object]) -> dict[str, object]:
    return {key: _safe_argument_value(key, value) for key, value in arguments.items()}


def _shell_command_contains_secret(command: ShellCommandRequest) -> bool:
    """Shell 草案会进入 SQLite checkpoint，因此比普通即时工具调用更严格。"""

    return any(_AUDIT_SECRET_PATTERN.search(argument) is not None for argument in command.argv)


def _risk_preview_digest(value: object) -> str | None:
    """将需要独立授权的命令预览绑定为稳定快照，避免授权被复用到新命令。"""

    if not isinstance(value, list):
        return None
    approvals: list[str] = []
    for preview in value:
        if not isinstance(preview, dict) or preview.get("requires_risk_approval") is not True:
            continue
        approval_sha256 = preview.get("approval_sha256")
        if not isinstance(approval_sha256, str) or len(approval_sha256) != 64:
            return None
        approvals.append(approval_sha256)
    if not approvals:
        return None
    payload = json.dumps(sorted(approvals), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _selected_patch_paths(preview: dict[str, object], selected: object) -> tuple[str, ...]:
    """校验审批时选择的路径只能是已预览补丁的非空子集。"""

    preview_paths_raw = preview.get("paths")
    if not isinstance(preview_paths_raw, list) or not preview_paths_raw:
        raise ValueError("PATCH_PREVIEW_PATHS_INVALID")
    preview_paths = tuple(item for item in preview_paths_raw if isinstance(item, str) and item)
    if len(preview_paths) != len(preview_paths_raw) or len(set(preview_paths)) != len(preview_paths):
        raise ValueError("PATCH_PREVIEW_PATHS_INVALID")
    if selected is None:
        # 兼容旧客户端：未提供选择时，显式视为接受整份已预览补丁。
        return preview_paths
    if not isinstance(selected, (list, tuple)) or not selected:
        raise ValueError("PATCH_SELECTION_EMPTY")
    selected_paths = tuple(item for item in selected if isinstance(item, str) and item)
    if len(selected_paths) != len(selected) or len(set(selected_paths)) != len(selected_paths):
        raise ValueError("PATCH_SELECTION_INVALID")
    if any(path not in preview_paths for path in selected_paths):
        raise ValueError("PATCH_SELECTION_OUTSIDE_PREVIEW")
    # 始终按预览顺序保存，避免相同集合因客户端排序不同而产生不同审批摘要。
    selected_set = set(selected_paths)
    return tuple(path for path in preview_paths if path in selected_set)


def _patch_selection_digest(preview: dict[str, object], selected_paths: tuple[str, ...]) -> str:
    """把已预览补丁哈希与文件选择共同冻结，应用前必须保持一致。"""

    preview_sha256 = preview.get("sha256")
    if not isinstance(preview_sha256, str) or len(preview_sha256) != 64:
        raise ValueError("PATCH_PREVIEW_DIGEST_INVALID")
    payload = json.dumps(
        {"patch_preview_sha256": preview_sha256, "selected_paths": list(selected_paths)},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _proposal_for_selected_paths(proposal: object, selected_paths: tuple[str, ...]) -> PatchProposal:
    """从已审批草案中构造只包含所选文件的可执行补丁。"""

    validated = PatchProposal.model_validate(proposal)
    selected_changes = [change for change in validated.changes if change.path in selected_paths]
    if len(selected_changes) != len(selected_paths):
        raise ValueError("PATCH_SELECTION_NOT_IN_PROPOSAL")
    return validated.model_copy(update={"changes": selected_changes})


def _safe_argument_value(key: str, value: object) -> object:
    """审计只保存受限、脱敏的工具参数摘要，避免 Shell argv 成为密钥旁路。"""

    normalized = key.lower()
    if any(marker in normalized for marker in ("token", "secret", "password", "api_key", "authorization")):
        return "[REDACTED]"
    if isinstance(value, str):
        return _AUDIT_SECRET_PATTERN.sub(r"\1\2[REDACTED]", value)[:2_048]
    if isinstance(value, list):
        return [_safe_argument_value(key, item) for item in value[:64]]
    if isinstance(value, dict):
        return {str(item_key)[:120]: _safe_argument_value(str(item_key), item_value) for item_key, item_value in list(value.items())[:32]}
    return value


def _tool_summary(payload: dict[str, object]) -> str:
    data = payload.get("data")
    if isinstance(data, dict):
        return f"{payload.get('message', '')}；结果字段：{', '.join(sorted(data)[:8])}。"
    return str(payload.get("message", "工具执行完成。"))


def _validation_issue_summary(error: ValidationError) -> list[dict[str, str]]:
    """为模型契约失败留下可定位、但不泄露模型内容的证据。"""
    return [
        {"field": ".".join(str(part) for part in item["loc"]), "rule": str(item["type"])}
        for item in error.errors(include_url=False)[:8]
    ]


def _usage_integer(payload: dict[str, object], *keys: str) -> int:
    """不同 OpenAI-compatible 服务字段不同；只接受非负整数，避免日志被异常值污染。"""

    for key in keys:
        value = payload.get(key)
        if isinstance(value, int) and value >= 0:
            return value
    return 0


def _model_usage_event(operation: str, usage: ModelUsage) -> dict[str, object]:
    """只记录供应商明确回传的聚合用量；未回传时显示不可用而不是伪造零成本。"""

    return {
        "type": "MODEL_USAGE",
        "operation": operation,
        "code": "MODEL_USAGE_REPORTED" if usage.reported else "MODEL_USAGE_UNAVAILABLE",
        **usage.to_dict(),
    }
