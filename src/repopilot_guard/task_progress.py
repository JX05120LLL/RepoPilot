"""将任务状态和审计事件收敛为稳定、无敏感信息的进度快照。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping


_COMMON_STAGES = (
    ("workspace", "准备工作区"),
    ("preflight", "运行预检"),
    ("context", "整理上下文"),
    ("research", "研究代码"),
    ("plan_approval", "计划审批"),
)
_CHANGE_STAGES = (
    ("execution_approval", "执行审批"),
    ("patch", "应用补丁"),
    ("verify", "Maven 验证"),
)
_REPORT_STAGE = ("report", "生成报告")

_STATUS_TO_STAGE = {
    "INTAKE": "workspace",
    "WORKSPACE": "workspace",
    "PREFLIGHT": "preflight",
    "MCP_BINDINGS": "context",
    "INGEST": "context",
    "RETRIEVE": "context",
    "CONTEXT_BROKER_READY": "context",
    "ANALYZE": "research",
    "RESEARCH_TOOLS": "research",
    "PLAN": "research",
    "PLAN_APPROVAL": "plan_approval",
    "EXECUTION_APPROVAL": "execution_approval",
    "PATCH": "patch",
    "VERIFY": "verify",
    "REVIEW": "report",
    "REPORT": "report",
}
_NODE_TO_STAGE = {
    "INTAKE": "workspace",
    "WORKSPACE": "workspace",
    "PREFLIGHT": "preflight",
    "MCP_BINDINGS": "context",
    "INGEST": "context",
    "RETRIEVE": "context",
    "ANALYZE": "research",
    "RESEARCH_TOOLS": "research",
    "PLAN": "plan_approval",
    "PLAN_APPROVAL": "plan_approval",
    "EXECUTION_APPROVAL": "execution_approval",
    "PATCH": "patch",
    "VERIFY": "verify",
    "REVIEW": "report",
    "REPORT": "report",
}


def build_task_progress(
    *,
    status: object,
    pending_approval: object = False,
    pending_approval_action: object = None,
    verdict: object = None,
    task_operation: object = "change",
    tool_events: object = (),
    verification: object = None,
) -> dict[str, object]:
    """生成给 API、CLI 和桌面端共用的只读任务阶段快照。

    输入只读取 Graph 已持久化的状态和事件类型，不读取模型消息、文件内容或工具参数，
    因而不会将仓库内容或凭证混入状态接口。
    """

    operation = str(task_operation).lower()
    change_task = operation != "research"
    stage_definitions = [*_COMMON_STAGES, *(_CHANGE_STAGES if change_task else ()), _REPORT_STAGE]
    stage_ids = [stage_id for stage_id, _ in stage_definitions]

    normalized_status = str(status or "").upper()
    normalized_verdict = str(verdict or "").upper()
    approval_action = str(pending_approval_action or "").upper()
    completed_nodes = _completed_nodes(tool_events)
    current_stage = _current_stage(
        normalized_status,
        bool(pending_approval),
        approval_action,
        normalized_verdict,
        completed_nodes,
        verification,
        stage_ids,
    )
    current_index = stage_ids.index(current_stage)
    # 审批是图外不可绕过的闸门。即使旧 checkpoint 出现终态字段，待审批任务也不能展示为已结束。
    terminal_kind = None if pending_approval else _terminal_kind(normalized_status, normalized_verdict)

    stages: list[dict[str, str]] = []
    for index, (stage_id, label) in enumerate(stage_definitions):
        stage_state = _stage_state(index, current_index, stage_id, current_stage, terminal_kind)
        stages.append({"id": stage_id, "label": label, "state": stage_state})

    return {
        "current_stage": current_stage,
        "summary": _summary(current_stage, terminal_kind),
        "terminal": terminal_kind is not None,
        "terminal_kind": terminal_kind,
        "stages": stages,
    }


def _completed_nodes(raw_events: object) -> tuple[str, ...]:
    if not isinstance(raw_events, Iterable) or isinstance(raw_events, (str, bytes, Mapping)):
        return ()
    nodes: list[str] = []
    for event in raw_events:
        if not isinstance(event, Mapping) or event.get("type") != "NODE_COMPLETED":
            continue
        node = event.get("node")
        if isinstance(node, str) and node in _NODE_TO_STAGE:
            nodes.append(node)
    return tuple(nodes)


def _current_stage(
    status: str,
    pending_approval: bool,
    approval_action: str,
    verdict: str,
    completed_nodes: tuple[str, ...],
    verification: object,
    stage_ids: list[str],
) -> str:
    if pending_approval:
        if approval_action == "EXECUTION_REVIEW":
            return "execution_approval"
        return "plan_approval"
    if status == "BLOCKED":
        for node in reversed(completed_nodes):
            return _NODE_TO_STAGE[node]
        return "workspace"
    if status in {"CANCELLED", "CANCELLATION_REQUESTED"}:
        for node in reversed(completed_nodes):
            return _NODE_TO_STAGE[node]
        return "workspace"
    if status == "REPORT" and verdict == "FAILED":
        return "verify" if "verify" in stage_ids and isinstance(verification, Mapping) else "report"
    return _STATUS_TO_STAGE.get(status, "workspace")


def _terminal_kind(status: str, verdict: str) -> str | None:
    if status == "BLOCKED" or verdict == "BLOCKED":
        return "blocked"
    if status in {"CANCELLED", "CANCELLATION_REQUESTED"}:
        return "cancelled"
    if status != "REPORT":
        return None
    if verdict == "PASSED":
        return "passed"
    if verdict == "FAILED":
        return "failed"
    return "unverified"


def _stage_state(index: int, current_index: int, stage_id: str, current_stage: str, terminal_kind: str | None) -> str:
    if terminal_kind is not None and stage_id == current_stage:
        return terminal_kind
    if index < current_index:
        return "completed"
    if stage_id == current_stage:
        return "current"
    return "pending"


def _summary(stage_id: str, terminal_kind: str | None) -> str:
    if terminal_kind == "passed":
        return "已记录真实 Diff 与 Maven 成功证据。"
    if terminal_kind == "failed":
        return "验证未通过，任务未被判定为修复成功。"
    if terminal_kind == "blocked":
        return "任务已按策略安全阻断，未继续执行后续动作。"
    if terminal_kind == "cancelled":
        return "任务已取消，后续动作没有继续执行。"
    if terminal_kind == "unverified":
        return "已输出只读研究结论，尚无补丁和 Maven 验证证据。"
    return {
        "workspace": "正在冻结工作区与 Git 基线。",
        "preflight": "正在检查本机依赖、模型服务和项目条件。",
        "context": "正在整理代码、文档和项目规则上下文。",
        "research": "正在通过受控只读工具研究代码。",
        "plan_approval": "计划已生成，等待你的审批。",
        "execution_approval": "执行范围已确定，等待你的二次审批。",
        "patch": "正在生成并原子应用结构化补丁。",
        "verify": "正在按白名单 Maven Recipe 验证修改。",
        "report": "正在汇总 Diff、验证结果和审计证据。",
    }[stage_id]
