"""将任务状态投影为可展示、可审计且不泄露内部异常的诊断摘要。"""

from __future__ import annotations


def build_task_diagnostic(
    *,
    status: object,
    verdict: object = None,
    pending_approval: object = False,
    error_summary: object = None,
    evidence_codes: object = (),
) -> dict[str, str]:
    """根据持久化任务事实生成跨 CLI/API/UI 共用的下一步说明。

    ``error_summary`` 只用于分类，绝不回显到诊断中，避免将异常详情、路径或凭证带到产品界面。
    """

    normalized_status = str(verdict or status or "").strip().upper()
    normalized_error = error_summary.strip().upper() if isinstance(error_summary, str) else ""
    normalized_evidence_codes = extract_diagnostic_codes(evidence_codes)

    if pending_approval:
        return _diagnostic(
            "warning",
            "PENDING_APPROVAL",
            "等待人工审批",
            "任务已暂停，审批前不会执行后续写入或验证动作。",
            "OPEN_APPROVAL",
        )
    if normalized_status == "PASSED":
        return _diagnostic(
            "success",
            "VERIFIED_REPAIR",
            "已通过真实验证",
            "真实 Diff 和受控 Maven Recipe 的成功证据均已生成，可进入代码审阅。",
            "OPEN_VERIFICATION",
        )
    if normalized_status == "FAILED":
        return _diagnostic(
            "danger",
            "PATCH_OR_VERIFICATION_FAILED",
            "补丁或验证未通过",
            "任务没有被标记为已修复；请先核验验证记录和真实 Diff，再决定是否发起新任务。",
            "OPEN_VERIFICATION",
        )
    if normalized_status == "CANCELLED":
        return _diagnostic(
            "warning",
            "TASK_CANCELLED",
            "任务已取消",
            "取消请求已生效，RepoPilot 没有继续执行后续动作。",
            "OPEN_TASK_EVIDENCE",
        )
    if normalized_status == "UNVERIFIED":
        return _diagnostic(
            "warning",
            "RESULT_UNVERIFIED",
            "结果尚未验证",
            "当前可以审阅计划和证据，但尚无足以证明修复成功的 Maven 验证记录。",
            "OPEN_PLAN",
        )
    if normalized_status == "BLOCKED":
        return _blocked_diagnostic(normalized_error, normalized_evidence_codes)
    return _diagnostic(
        "neutral",
        "TASK_STATUS_AVAILABLE",
        "任务状态已同步",
        "RepoPilot 正在以持久化任务状态和审计事件更新当前视图。",
        "OPEN_TASK_EVIDENCE",
    )


def _blocked_diagnostic(error: str, evidence_codes: set[str]) -> dict[str, str]:
    if "REPOSITORY_PREFLIGHT_FAILED" in evidence_codes:
        return _diagnostic(
            "danger",
            "PROJECT_PREFLIGHT_REQUIRED",
            "项目预检条件未满足",
            "当前项目不符合本次操作所需的预检条件。只读研究可在含源码的聚合目录继续；安全隔离修复需要 Git 基线，代码修改与验证需要定位具体模块。",
            "OPEN_TASK_EVIDENCE",
        )
    if any(code.startswith(("CHAT_", "EMBEDDING_", "QDRANT_")) for code in evidence_codes):
        return _diagnostic(
            "danger",
            "RUNTIME_CONFIGURATION_REQUIRED",
            "Agent 运行配置未就绪",
            "RepoPilot 未调用后续高风险工具。请检查模型、Embedding、Qdrant 等本机运行配置后重新发起任务。",
            "OPEN_RUNTIME_CONFIGURATION",
        )
    if "LEASE" in error:
        return _diagnostic(
            "danger",
            "TASK_LEASE_EXPIRED",
            "任务执行租约已过期",
            "执行进程未在规定时间内续约，任务已停止；请核验已有证据后重新发起任务。",
            "OPEN_TASK_EVIDENCE",
        )
    if "GIT" in error or "WORKTREE" in error or "BASELINE" in error:
        return _diagnostic(
            "danger",
            "GIT_WORKSPACE_CHECK_REQUIRED",
            "Git 工作区条件未满足",
            "RepoPilot 未继续执行。请检查项目是否为有效 Git 仓库、基线提交和工作区状态是否满足当前模式。",
            "OPEN_TASK_EVIDENCE",
        )
    if any(marker in error for marker in ("CONFIGURATION", "API_KEY", "QDRANT", "EMBEDDING", "CHAT_MODEL")):
        return _diagnostic(
            "danger",
            "RUNTIME_CONFIGURATION_REQUIRED",
            "Agent 运行配置未就绪",
            "RepoPilot 未调用后续高风险工具。请检查模型、Embedding、Qdrant 等本机运行配置后重新发起任务。",
            "OPEN_RUNTIME_CONFIGURATION",
        )
    if "PREFLIGHT" in error or "预检" in error:
        return _diagnostic(
            "danger",
            "PREFLIGHT_BLOCKED",
            "环境预检未通过",
            "任务在代码研究或修改前已停止。请检查项目条件和本机 Agent 运行配置后重新发起任务。",
            "OPEN_RUNTIME_CONFIGURATION",
        )
    if any(marker in error for marker in ("POLICY", "SENSITIVE", "PERMISSION", "PATH", "OUTSIDE")):
        return _diagnostic(
            "danger",
            "POLICY_GUARD_BLOCKED",
            "安全策略拒绝了该操作",
            "PolicyGuard 已阻止越权、敏感路径或不受控的操作；任务没有继续执行。",
            "OPEN_TASK_EVIDENCE",
        )
    if "TASK_RUNTIME_FAILED" in error:
        return _diagnostic(
            "danger",
            "TASK_RUNTIME_FAILURE",
            "任务运行时异常已安全停止",
            "RepoPilot 已停止后续动作并保留脱敏证据。请核验任务证据和项目条件后重新发起任务。",
            "OPEN_TASK_EVIDENCE",
        )
    return _diagnostic(
        "danger",
        "TASK_BLOCKED",
        "任务已被安全阻断",
        "RepoPilot 没有继续执行不满足前置条件或不符合策略的操作。请先核验已有证据。",
        "OPEN_TASK_EVIDENCE",
    )


def extract_diagnostic_codes(events: object) -> set[str]:
    """仅提取受控事件中的错误码，不读取消息、命令、路径或模型输出。"""

    if isinstance(events, (set, frozenset)):
        codes: set[str] = set()
        for code in events:
            _add_code(codes, code)
        return codes
    if not isinstance(events, (list, tuple)):
        return set()
    codes: set[str] = set()
    for event in events:
        payload = getattr(event, "payload", event)
        if not isinstance(payload, dict):
            continue
        _add_code(codes, payload.get("code"))
        checks = payload.get("checks")
        if not isinstance(checks, list):
            continue
        for check in checks:
            if isinstance(check, dict) and str(check.get("status", "")).upper() == "BLOCKED":
                _add_code(codes, check.get("code"))
    return codes


def _add_code(codes: set[str], value: object) -> None:
    if isinstance(value, str) and value and len(value) <= 128:
        codes.add(value.upper())


def _diagnostic(
    tone: str,
    code: str,
    title: str,
    summary: str,
    recommended_action: str,
) -> dict[str, str]:
    return {
        "tone": tone,
        "code": code,
        "title": title,
        "summary": summary,
        "recommended_action": recommended_action,
    }
