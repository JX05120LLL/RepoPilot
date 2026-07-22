"""已授权项目的只读可用性诊断。"""

from __future__ import annotations

from repopilot_guard.preflight import PreflightInspector
from repopilot_guard.project_registry import ProjectRecord
from repopilot_guard.workspace import GitClient, GitCommandError


def diagnose_project(project: ProjectRecord) -> dict[str, object]:
    """按实际工作区规则说明两种产品模式和 Java/Maven Profile 的可用性。"""

    preflight = PreflightInspector().inspect(project.root_path)
    safe_mode: dict[str, object]
    baseline_commit: str | None = None
    dirty_entries: tuple[str, ...] = ()
    if not preflight.is_git_repository:
        safe_mode = {
            "status": "BLOCKED",
            "code": "GIT_REPOSITORY_REQUIRED",
            "message": "安全隔离修复需要 Git 仓库和可冻结的基线。",
        }
    else:
        try:
            client = GitClient()
            baseline_commit = client.head_commit(project.root_path)
            dirty_entries = client.status_porcelain(project.root_path)
            if dirty_entries:
                safe_mode = {
                    "status": "BLOCKED",
                    "code": "DIRTY_SOURCE_BLOCKED",
                    "message": "源仓库存在未提交改动；默认安全模式不会自动迁移、暂存或清理改动。",
                    "dirty_entry_count": len(dirty_entries),
                }
            else:
                safe_mode = {
                    "status": "READY",
                    "code": "SAFE_ISOLATED_READY",
                    "message": "Git 基线干净，可创建 detached worktree 进行隔离修复。",
                }
        except GitCommandError:
            safe_mode = {
                "status": "BLOCKED",
                "code": "GIT_BASELINE_UNAVAILABLE",
                "message": "无法读取 Git HEAD；请先创建至少一个提交后再使用安全隔离修复。",
            }

    full_mode = {
        "status": "READY",
        "code": "FULL_LOCAL_READY" if preflight.is_git_repository else "FULL_LOCAL_RESEARCH_ONLY",
        "message": (
            "完全本机控制可在 Local 工作区执行已实现的受控工具，仍需按任务二次确认。"
            if preflight.is_git_repository
            else "非 Git 项目可在完全本机控制下进行研究；无法创建 Worktree 或提供可信 Git Diff。"
        ),
    }
    java_profile = {
        "status": "READY" if preflight.has_pom_xml else "PARTIAL",
        "code": "JAVA_MAVEN_PROFILE_READY" if preflight.has_pom_xml else "MAVEN_POM_NOT_FOUND",
        "has_pom_xml": preflight.has_pom_xml,
        "java_source_root": str(preflight.java_source_root) if preflight.java_source_root else None,
        "maven_wrapper": str(preflight.maven_wrapper) if preflight.maven_wrapper else None,
        "warnings": list(preflight.warnings),
    }
    recommended_mode = "safe-isolated" if safe_mode["status"] == "READY" else "full-local"
    return {
        "status": "READY",
        "code": "PROJECT_DIAGNOSIS_READY",
        "project": project.to_dict(),
        "recommended_task_mode": recommended_mode,
        "task_modes": {"safe_isolated": safe_mode, "full_local": full_mode},
        "git": {
            "is_repository": preflight.is_git_repository,
            "baseline_commit": baseline_commit,
            "dirty_entry_count": len(dirty_entries),
        },
        "profiles": {"java_maven": java_profile},
        "next_actions": (
            ["可以使用 task start 的默认安全隔离修复模式。"]
            if recommended_mode == "safe-isolated"
            else ["如需安全隔离修复，请初始化 Git 并创建至少一个干净提交。", "如仅需研究，可使用完整本机控制并完成任务级确认。"]
        ),
    }
