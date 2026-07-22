"""Git 基线快照与 detached worktree 隔离。"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from repopilot_guard.models import TaskRequest, WorkspaceMode
from repopilot_guard.permissions import PermissionGrant
from repopilot_guard.policy import PolicyGuard, ToolName


class GitCommandError(RuntimeError):
    """Git 命令失败时保留安全、简短的上下文。"""


class GitClient:
    """只通过 argv 调用 Git，避免 Shell 拼接。"""

    def run(self, repository: Path, *arguments: str, input_text: str | None = None) -> str:
        completed = subprocess.run(
            ("git", "-C", str(repository), *arguments),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            input=input_text,
        )
        if completed.returncode != 0:
            message = completed.stderr.strip() or "Git 命令执行失败。"
            raise GitCommandError(message[:500])
        return completed.stdout

    def head_commit(self, repository: Path) -> str:
        return self.run(repository, "rev-parse", "HEAD").strip()

    def resolve_commit(self, repository: Path, reference: str) -> str:
        return self.run(repository, "rev-parse", "--verify", f"{reference}^{{commit}}").strip()

    def status_porcelain(self, repository: Path) -> tuple[str, ...]:
        return tuple(line for line in self.run(repository, "status", "--porcelain=v1").splitlines() if line)

    def diff_binary(self, repository: Path) -> bytes:
        return self.run(repository, "diff", "--binary", "HEAD").encode("utf-8")

    def untracked_files(self, repository: Path) -> tuple[Path, ...]:
        output = self.run(repository, "ls-files", "--others", "--exclude-standard", "-z")
        return tuple(Path(item) for item in output.split("\0") if item)

    def working_files(self, repository: Path) -> tuple[Path, ...]:
        """返回已跟踪和未被 Git 忽略的文件，跳过 .venv 等构建产物。"""

        output = self.run(repository, "ls-files", "-co", "--exclude-standard", "-z")
        return tuple(Path(item) for item in output.split("\0") if item)

    def add_detached_worktree(self, repository: Path, destination: Path, commit: str) -> None:
        self.run(repository, "worktree", "add", "--detach", str(destination), commit)

    def apply_binary_patch(self, repository: Path, patch: bytes) -> None:
        self.run(repository, "apply", "--binary", "--whitespace=nowarn", "-", input_text=patch.decode("utf-8"))

    def create_branch(self, repository: Path, branch_name: str) -> None:
        self.run(repository, "check-ref-format", "--branch", branch_name)
        self.run(repository, "switch", "-c", branch_name)


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    """创建 worktree 前后的源仓库基线信息。"""

    repository: Path
    head_commit: str
    dirty_entries: tuple[str, ...]
    diff_sha256: str
    content_sha256: str

    @property
    def is_dirty(self) -> bool:
        return bool(self.dirty_entries)

    def to_dict(self) -> dict[str, object]:
        return {
            "repository": str(self.repository),
            "head_commit": self.head_commit,
            "is_dirty": self.is_dirty,
            "dirty_entries": list(self.dirty_entries),
            "diff_sha256": self.diff_sha256,
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True, slots=True)
class WorkspacePreparationResult:
    """worktree 准备结果，失败同样保留可审计的基线。"""

    status: str
    code: str
    permission: PermissionGrant
    snapshot: RepositorySnapshot | None
    workspace_path: Path | None
    message: str
    created_at: datetime
    source_unchanged: bool
    evidence_events_path: Path | None = None
    mode: WorkspaceMode = WorkspaceMode.WORKTREE
    base_commit: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "code": self.code,
            "permission": self.permission.to_dict(),
            "snapshot": self.snapshot.to_dict() if self.snapshot else None,
            "workspace_path": str(self.workspace_path) if self.workspace_path else None,
            "message": self.message,
            "created_at": self.created_at.isoformat(),
            "source_unchanged": self.source_unchanged,
            "evidence_events_path": str(self.evidence_events_path) if self.evidence_events_path else None,
            "mode": self.mode.value,
            "base_commit": self.base_commit,
        }


class WorkspaceManager:
    """为每个任务创建保留的 detached worktree，绝不改写源仓库文件。"""

    def __init__(self, git_client: GitClient | None = None) -> None:
        self._git = git_client or GitClient()

    def prepare(self, request: TaskRequest, permission: PermissionGrant) -> WorkspacePreparationResult:
        created_at = datetime.now(timezone.utc)
        selection = request.workspace_selection
        try:
            before = self.snapshot(request.repository)
        except GitCommandError:
            if selection.mode is WorkspaceMode.LOCAL and permission.is_full_access:
                fingerprint = self._filesystem_digest(request.repository)
                return WorkspacePreparationResult(
                    status="READY",
                    code="LOCAL_NON_GIT_WORKSPACE_READY",
                    permission=permission,
                    snapshot=None,
                    workspace_path=request.repository,
                    message="已绑定非 Git 的 Local 工作区；允许只读研究，但无法提供 Worktree 或 Git Diff 证据。",
                    created_at=created_at,
                    source_unchanged=False,
                    mode=selection.mode,
                    base_commit=f"non-git-{fingerprint[:16]}",
                )
            return WorkspacePreparationResult(
                status="BLOCKED",
                code="GIT_BASELINE_UNAVAILABLE",
                permission=permission,
                snapshot=None,
                workspace_path=None,
                message="无法读取 Git HEAD；请确认源仓库至少包含一个已提交的提交。",
                created_at=created_at,
                source_unchanged=True,
            )
        try:
            base_commit = self._git.resolve_commit(request.repository, selection.start_ref)
        except GitCommandError:
            return WorkspacePreparationResult(
                status="BLOCKED",
                code="START_REF_UNAVAILABLE",
                permission=permission,
                snapshot=before,
                workspace_path=None,
                message="指定的起始分支或提交不存在，未创建工作区。",
                created_at=created_at,
                source_unchanged=True,
                mode=selection.mode,
            )
        if selection.mode is WorkspaceMode.LOCAL:
            return WorkspacePreparationResult(
                status="READY",
                code="LOCAL_WORKSPACE_READY",
                permission=permission,
                snapshot=before,
                workspace_path=request.repository,
                message="已绑定 Local 工作区；本阶段不会写入源仓库。",
                created_at=created_at,
                source_unchanged=True,
                mode=selection.mode,
                base_commit=base_commit,
            )

        if before.is_dirty and not selection.include_uncommitted_changes:
            return WorkspacePreparationResult(
                status="BLOCKED",
                code="DIRTY_SOURCE_BLOCKED",
                permission=permission,
                snapshot=before,
                workspace_path=None,
                message="源仓库存在未提交改动；请显式选择迁移改动后再创建 worktree。",
                created_at=created_at,
                source_unchanged=True,
                mode=selection.mode,
                base_commit=base_commit,
            )

        destination = request.output_root / request.task_id / "worktree"
        if destination.exists():
            return WorkspacePreparationResult(
                status="BLOCKED",
                code="WORKSPACE_EXISTS",
                permission=permission,
                snapshot=before,
                workspace_path=None,
                message="任务 worktree 路径已存在；为了避免覆盖，未执行任何操作。",
                created_at=created_at,
                source_unchanged=True,
            )

        destination.parent.mkdir(parents=True, exist_ok=True)
        self._git.add_detached_worktree(request.repository, destination, base_commit)
        if before.is_dirty and selection.include_uncommitted_changes:
            try:
                self._migrate_uncommitted_changes(request.repository, destination, permission)
            except (GitCommandError, OSError, UnicodeDecodeError):
                return WorkspacePreparationResult(
                    status="BLOCKED",
                    code="DIRTY_MIGRATION_FAILED",
                    permission=permission,
                    snapshot=before,
                    workspace_path=destination,
                    message="worktree 已创建，但未提交改动迁移失败；已保留目录供审查。",
                    created_at=created_at,
                    source_unchanged=True,
                    mode=selection.mode,
                    base_commit=base_commit,
                )
        try:
            after = self.snapshot(request.repository)
        except GitCommandError:
            return WorkspacePreparationResult(
                status="BLOCKED",
                code="SOURCE_VERIFICATION_FAILED",
                permission=permission,
                snapshot=before,
                workspace_path=destination,
                message="worktree 已创建，但无法再次读取源仓库基线；已阻断后续操作。",
                created_at=created_at,
                source_unchanged=False,
                mode=selection.mode,
                base_commit=base_commit,
            )
        source_unchanged = before == after
        if not source_unchanged:
            return WorkspacePreparationResult(
                status="BLOCKED",
                code="SOURCE_CHANGED",
                permission=permission,
                snapshot=after,
                workspace_path=destination,
                message="源仓库基线发生意外变化，已阻断后续操作；保留 worktree 供审查。",
                created_at=created_at,
                source_unchanged=False,
                mode=selection.mode,
                base_commit=base_commit,
            )

        message = "已创建并保留 detached worktree。"
        if before.is_dirty:
            message += " 已按用户选择迁移允许范围内的未提交改动。"
        return WorkspacePreparationResult(
            status="READY",
            code="WORKSPACE_READY",
            permission=permission,
            snapshot=before,
            workspace_path=destination,
            message=message,
            created_at=created_at,
            source_unchanged=True,
            mode=selection.mode,
            base_commit=base_commit,
        )

    def create_branch(self, workspace_path: Path, branch_name: str) -> dict[str, object]:
        """将 detached worktree 转为用户显式命名的分支。"""

        workspace = workspace_path.expanduser().resolve()
        self._git.create_branch(workspace, branch_name)
        return {"status": "READY", "code": "BRANCH_CREATED", "workspace_path": str(workspace), "branch": branch_name}

    def status(self, workspace_path: Path) -> dict[str, object]:
        workspace = workspace_path.expanduser().resolve()
        return {
            "status": "READY",
            "workspace_path": str(workspace),
            "head_commit": self._git.head_commit(workspace),
            "branch": self._git.run(workspace, "rev-parse", "--abbrev-ref", "HEAD").strip(),
            "dirty_entries": list(self._git.status_porcelain(workspace)),
        }

    def handoff_to_local(
        self,
        worktree_path: Path,
        local_repository: Path,
        expected_local_snapshot: RepositorySnapshot,
        permission: PermissionGrant,
    ) -> dict[str, object]:
        """将已验证 worktree diff 显式交接给 Local，绝不自动删除 worktree。"""

        if not permission.is_full_access:
            return {"status": "BLOCKED", "code": "LOCAL_HANDOFF_REQUIRES_FULL", "message": "交接到 Local 需要完全本机控制确认。"}
        local = local_repository.expanduser().resolve()
        worktree = worktree_path.expanduser().resolve()
        try:
            current = self.snapshot(local)
        except GitCommandError:
            return {"status": "BLOCKED", "code": "LOCAL_BASELINE_UNAVAILABLE", "message": "无法读取 Local 基线。"}
        if current != expected_local_snapshot:
            return {"status": "BLOCKED", "code": "LOCAL_BASELINE_CHANGED", "message": "Local 基线已变化，拒绝覆盖用户改动。"}
        try:
            patch = self._git.diff_binary(worktree)
        except GitCommandError:
            return {"status": "BLOCKED", "code": "WORKTREE_DIFF_UNAVAILABLE", "message": "无法读取 worktree diff。"}
        if not patch:
            return {"status": "BLOCKED", "code": "WORKTREE_DIFF_EMPTY", "message": "worktree 没有可交接的修改。"}
        try:
            self._git.apply_binary_patch(local, patch)
        except GitCommandError as error:
            return {"status": "BLOCKED", "code": "LOCAL_HANDOFF_CONFLICT", "message": str(error)}
        return {
            "status": "READY",
            "code": "LOCAL_HANDOFF_APPLIED",
            "message": "已显式应用 worktree diff 到 Local；worktree 已保留。",
            "worktree_path": str(worktree),
            "local_repository": str(local),
        }

    def snapshot(self, repository: Path) -> RepositorySnapshot:
        repository = repository.expanduser().resolve()
        diff = self._git.diff_binary(repository)
        return RepositorySnapshot(
            repository=repository,
            head_commit=self._git.head_commit(repository),
            dirty_entries=self._git.status_porcelain(repository),
            diff_sha256=hashlib.sha256(diff).hexdigest(),
            content_sha256=self._content_digest(repository),
        )

    def _content_digest(self, repository: Path) -> str:
        digest = hashlib.sha256()
        for relative_path in sorted(self._git.working_files(repository)):
            path = repository / relative_path
            if not path.is_file():
                continue
            relative = relative_path.as_posix().encode("utf-8")
            digest.update(relative)
            digest.update(b"\0")
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(64 * 1024), b""):
                    digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _filesystem_digest(repository: Path) -> str:
        """为非 Git Local 工作区生成只读检索隔离标识，不替代 Git 提交。"""
        digest = hashlib.sha256()
        for path in sorted(repository.rglob("*")):
            if not path.is_file() or ".git" in path.parts:
                continue
            relative = path.relative_to(repository).as_posix().encode("utf-8")
            digest.update(relative)
            digest.update(b"\0")
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(64 * 1024), b""):
                    digest.update(block)
        return digest.hexdigest()

    def _migrate_uncommitted_changes(self, source: Path, destination: Path, permission: PermissionGrant) -> None:
        patch = self._git.diff_binary(source)
        if patch:
            self._git.apply_binary_patch(destination, patch)
        guard = PolicyGuard(source, permission)
        for relative_path in self._git.untracked_files(source):
            source_path = source / relative_path
            if not source_path.is_file() or not guard.check_path(ToolName.READ_FILE, source_path).allowed:
                continue
            target_path = destination / relative_path
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path)
