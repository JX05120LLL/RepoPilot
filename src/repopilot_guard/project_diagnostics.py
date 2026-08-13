"""已授权项目的只读可用性诊断。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from repopilot_guard.models import TaskMode, TaskOperation
from repopilot_guard.preflight import PreflightInspector
from repopilot_guard.project_profiles import profile_payload
from repopilot_guard.profile_runtime import ProfileRuntimeInspector
from repopilot_guard.project_registry import ProjectRecord
from repopilot_guard.workspace import GitClient, GitCommandError


@dataclass(frozen=True, slots=True)
class TaskAdmission:
    """创建任务前冻结的项目能力判断，不依赖模型或 Graph。"""

    ready: bool
    code: str
    message: str
    allowed_operations: tuple[TaskOperation, ...]

    @property
    def status(self) -> str:
        return "READY" if self.ready else "BLOCKED"

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "allowed_operations": [operation.value for operation in self.allowed_operations],
        }


def assess_task_admission(
    repository: Path,
    task_mode: TaskMode,
    operation: TaskOperation,
    *,
    include_uncommitted_changes: bool = False,
) -> TaskAdmission:
    """在加载模型和创建任务记录前拒绝项目能力不支持的组合。"""

    preflight = PreflightInspector().inspect(repository)
    if not preflight.repository.is_dir():
        return TaskAdmission(False, "REPOSITORY_NOT_FOUND", "项目目录不存在或不可读取。", ())

    if task_mode is TaskMode.SAFE_ISOLATED:
        if not preflight.is_git_repository:
            return TaskAdmission(
                False,
                "GIT_REPOSITORY_REQUIRED",
                "安全隔离任务需要 Git 仓库；请先初始化 Git，或改用完全本机的仅研究任务。",
                (),
            )
        try:
            client = GitClient()
            client.head_commit(preflight.repository)
            dirty_entries = client.status_porcelain(preflight.repository)
        except GitCommandError:
            return TaskAdmission(
                False,
                "GIT_BASELINE_UNAVAILABLE",
                "安全隔离任务需要至少一个可解析的 Git 提交；请先创建基线提交。",
                (),
            )
        if dirty_entries and not include_uncommitted_changes:
            return TaskAdmission(
                False,
                "DIRTY_SOURCE_BLOCKED",
                "源仓库存在未提交改动；请先提交改动，或显式选择迁移未提交改动。",
                (),
            )
        allowed = (TaskOperation.CHANGE, TaskOperation.RESEARCH)
        return TaskAdmission(
            True,
            "SAFE_ISOLATED_TASK_READY",
            "项目具备安全隔离任务的基础条件；Git 基线将在工作区节点再次校验。",
            allowed,
        )

    # 完全本机控制不依赖 Git：没有可解析 HEAD 时，后续工作区会冻结文件哈希并
    # 生成统一文本 Diff。它仍要求任务级完全权限确认，不能替代安全隔离 Worktree。
    baseline_available = False
    if preflight.is_git_repository:
        try:
            GitClient().head_commit(preflight.repository)
            baseline_available = True
        except GitCommandError:
            pass
    allowed = (TaskOperation.CHANGE, TaskOperation.RESEARCH)
    return TaskAdmission(
        True,
        "FULL_LOCAL_TASK_READY",
        "项目具备当前完全本机任务的基础条件；执行仍受已注册工具和审批约束。",
        allowed,
    )


def diagnose_project(
    project: ProjectRecord,
    *,
    runtime_inspector: ProfileRuntimeInspector | None = None,
) -> dict[str, object]:
    """按实际工作区规则说明两种产品模式和已识别技术栈的可用性。"""

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

    full_change_ready = baseline_commit is not None
    full_mode = {
        "status": "READY",
        "code": "FULL_LOCAL_READY" if full_change_ready else "FULL_LOCAL_FILE_SNAPSHOT_READY",
        "message": (
            "完全本机控制可在 Local 工作区执行已实现的受控工具，仍需按任务二次确认。"
            if full_change_ready
            else (
                "当前 Git 仓库尚无可解析提交；完全本机控制会冻结文件快照并生成文本 Diff，"
                "但不能创建 Worktree、分支或 Git 级交接记录。"
                if preflight.is_git_repository
                else "非 Git 项目可在完全本机控制下执行受控分析和修改；系统会冻结文件快照并生成文本 Diff，"
                "但不能创建 Worktree、分支或 Git 级交接记录。"
            )
        ),
    }
    safe_mode["allowed_operations"] = (
        [operation.value for operation in TaskOperation]
        if safe_mode["status"] == "READY"
        else []
    )
    full_mode["allowed_operations"] = [operation.value for operation in TaskOperation]
    profiles = profile_payload(project.root_path)
    inspector = runtime_inspector or ProfileRuntimeInspector()
    for profile_id, profile in profiles.items():
        profile["runtime"] = inspector.inspect(profile_id, project.root_path).to_dict()
    if "java_maven" in profiles:
        profiles["java_maven"].update(
            {
                "has_pom_xml": preflight.has_pom_xml,
                "java_source_root": str(preflight.java_source_root) if preflight.java_source_root else None,
                "maven_wrapper": str(preflight.maven_wrapper) if preflight.maven_wrapper else None,
                "warnings": list(preflight.warnings),
            }
        )
    if "java_gradle" in profiles:
        profiles["java_gradle"].update(
            {
                "has_gradle_build": preflight.has_gradle_build,
                "gradle_wrapper": str(preflight.gradle_wrapper) if preflight.gradle_wrapper else None,
                "warnings": list(preflight.warnings),
            }
        )
    if "python_pytest" in profiles:
        profiles["python_pytest"].update(
            {
                "has_pytest_project": preflight.has_pytest_project,
                "warnings": list(preflight.warnings),
            }
        )
    for profile_id in ("node_npm", "node_pnpm"):
        if profile_id in profiles:
            profiles[profile_id].update(
                {
                    "has_node_project": preflight.has_node_project,
                    "package_manager": "pnpm" if profile_id == "node_pnpm" else "npm",
                    "warnings": list(preflight.warnings),
                }
            )
    recommended_mode = "safe-isolated" if safe_mode["status"] == "READY" else "full-local"
    recommended_operation = TaskOperation.CHANGE.value
    return {
        "status": "READY",
        "code": "PROJECT_DIAGNOSIS_READY",
        "project": project.to_dict(),
        "recommended_task_mode": recommended_mode,
        "recommended_task_operation": recommended_operation,
        "task_modes": {"safe_isolated": safe_mode, "full_local": full_mode},
        "git": {
            "is_repository": preflight.is_git_repository,
            "baseline_commit": baseline_commit,
            "dirty_entry_count": len(dirty_entries),
        },
        "profiles": profiles,
        "next_actions": (
            ["可以使用 task start 的默认安全隔离修复模式。"]
            if recommended_mode == "safe-isolated"
            else [
                "如需安全隔离修复，请初始化 Git 并创建至少一个干净提交。",
                "非 Git 或无提交项目可切换到完全本机控制：系统将使用文件快照与文本 Diff，不会自动初始化或提交 Git。",
            ]
        ),
    }
