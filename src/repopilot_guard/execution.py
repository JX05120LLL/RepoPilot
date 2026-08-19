"""阶段五的结构化补丁与验证运行时。"""

from __future__ import annotations

import os
from difflib import unified_diff
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, Field

from repopilot_guard.permissions import PermissionGrant
from repopilot_guard.policy import GradleRecipeName, MavenRecipeName, NodeRecipeName, NoVerificationRecipeName, PolicyGuard, PytestRecipeName, ToolName
from repopilot_guard.recipes import GradleExecutionResult, GradleRecipeRunner, MavenExecutionResult, MavenRecipeRunner, NodeExecutionResult, NodeRecipeRunner, PytestExecutionResult, PytestRecipeRunner
from repopilot_guard.workspace import GitClient, GitCommandError


MAX_PATCH_FILES = 8
MAX_FILE_BYTES = 256 * 1024
MAX_PATCH_BYTES = 512 * 1024
MAX_PATCH_REPAIR_SNAPSHOT_BYTES = 32 * 1024


class PatchFileChange(BaseModel):
    """一个可验证的文本替换，不能表达任意命令或二进制 patch。"""

    path: str = Field(min_length=1)
    expected_old_text: str = Field(min_length=1)
    new_text: str


class PatchProposal(BaseModel):
    summary: str = Field(min_length=1)
    changes: list[PatchFileChange] = Field(min_length=1, max_length=MAX_PATCH_FILES)
    recipe: MavenRecipeName | GradleRecipeName | PytestRecipeName | NodeRecipeName | NoVerificationRecipeName = MavenRecipeName.TEST
    test_class: str | None = None


@dataclass(frozen=True, slots=True)
class PatchApplyResult:
    status: str
    code: str
    message: str
    changed_paths: tuple[str, ...] = ()
    diff: str = ""
    failed_path: str | None = None


@dataclass(frozen=True, slots=True)
class VerificationResult:
    result: MavenExecutionResult | GradleExecutionResult | PytestExecutionResult | NodeExecutionResult

    @property
    def status(self) -> str:
        return self.result.status


@dataclass(frozen=True, slots=True)
class _PreparedPatchChange:
    """已在内存校验的单文件替换；预览与实际写入必须共享同一校验规则。"""

    target: Path
    relative_path: str
    original: bytes
    updated: bytes


class StructuredPatchApplier:
    """先在内存中验证全部替换，再写入；写入异常会尽力回滚已替换文件。"""

    def __init__(self, git_client: GitClient | None = None) -> None:
        self._git = git_client or GitClient()

    def preview(
        self,
        workspace_root: Path,
        proposal: PatchProposal,
        permission: PermissionGrant,
        candidate_files: set[str] | None = None,
    ) -> PatchApplyResult:
        """只在内存中生成补丁预览，供执行审批绑定具体文件和 Diff。"""

        root, prepared_or_error = self._prepare(workspace_root, proposal, permission, candidate_files)
        if isinstance(prepared_or_error, PatchApplyResult):
            return prepared_or_error
        prepared = prepared_or_error
        diff = "".join(
            line
            for item in prepared
            for line in unified_diff(
                item.original.decode("utf-8").splitlines(keepends=True),
                item.updated.decode("utf-8").splitlines(keepends=True),
                fromfile=f"a/{item.relative_path}",
                tofile=f"b/{item.relative_path}",
            )
        )
        return PatchApplyResult(
            "READY",
            "PATCH_PREVIEW_READY",
            "结构化补丁已通过预校验，等待执行审批。",
            tuple(item.relative_path for item in prepared),
            diff,
        )

    def apply(
        self,
        workspace_root: Path,
        proposal: PatchProposal,
        permission: PermissionGrant,
        candidate_files: set[str] | None = None,
    ) -> PatchApplyResult:
        root, prepared_or_error = self._prepare(workspace_root, proposal, permission, candidate_files)
        if isinstance(prepared_or_error, PatchApplyResult):
            return prepared_or_error
        prepared = prepared_or_error

        replaced: list[_PreparedPatchChange] = []
        try:
            for item in prepared:
                temporary = item.target.with_name(f".{item.target.name}.repopilot.tmp")
                temporary.write_bytes(item.updated)
                os.replace(temporary, item.target)
                replaced.append(item)
        except OSError as error:
            for item in reversed(replaced):
                item.target.write_bytes(item.original)
            return PatchApplyResult("FAILED", "PATCH_WRITE_FAILED", f"写入补丁失败且已尝试回滚：{error}")

        diff = self._render_diff(root, prepared)
        return PatchApplyResult("READY", "PATCH_APPLIED", "结构化补丁已应用，尚未验证。", tuple(item.relative_path for item in prepared), diff)

    def _render_diff(self, root: Path, prepared: list[_PreparedPatchChange]) -> str:
        """优先保留 Git 二进制 Diff；非 Git 项目回退到同一批已校验文本的统一 Diff。"""

        try:
            return self._git.run(root, "diff", "--binary", "HEAD")
        except GitCommandError:
            return "".join(
                line
                for item in prepared
                for line in unified_diff(
                    item.original.decode("utf-8").splitlines(keepends=True),
                    item.updated.decode("utf-8").splitlines(keepends=True),
                    fromfile=f"a/{item.relative_path}",
                    tofile=f"b/{item.relative_path}",
                )
            )

    @staticmethod
    def _prepare(
        workspace_root: Path,
        proposal: PatchProposal,
        permission: PermissionGrant,
        candidate_files: set[str] | None,
    ) -> tuple[Path, list[_PreparedPatchChange] | PatchApplyResult]:
        root = workspace_root.expanduser().resolve()
        guard = PolicyGuard(root, permission)
        prepared: list[_PreparedPatchChange] = []
        total_bytes = 0
        seen: set[Path] = set()

        for change in proposal.changes:
            target = (root / change.path).resolve()
            decision = guard.check_path(ToolName.APPLY_PATCH, target)
            if not decision.allowed:
                return root, PatchApplyResult("BLOCKED", decision.audit_code, decision.reason)
            if candidate_files is not None and change.path not in candidate_files:
                return root, PatchApplyResult("BLOCKED", "PATCH_TARGET_NOT_RESEARCHED", f"补丁目标未出现在研究证据中：{change.path}")
            if target in seen:
                return root, PatchApplyResult("BLOCKED", "PATCH_DUPLICATE_TARGET", f"同一文件只能替换一次：{change.path}")
            seen.add(target)
            if not target.is_file():
                return root, PatchApplyResult("BLOCKED", "PATCH_TARGET_NOT_FILE", f"补丁目标不是文件：{change.path}")
            if target.stat().st_size > MAX_FILE_BYTES:
                return root, PatchApplyResult("BLOCKED", "PATCH_FILE_TOO_LARGE", f"补丁目标超过 {MAX_FILE_BYTES} 字节：{change.path}")
            try:
                # 保留 CRLF/LF 原样，避免模型使用 LF 时把整个 Windows 文件重写为 LF。
                original_text = target.read_bytes().decode("utf-8")
            except UnicodeDecodeError:
                return root, PatchApplyResult("BLOCKED", "PATCH_BINARY_FILE", f"补丁目标不是 UTF-8 文本：{change.path}")
            expected_old_text = _adapt_line_endings(change.expected_old_text, original_text)
            new_text = _adapt_line_endings(change.new_text, original_text)
            matches = original_text.count(expected_old_text)
            if matches != 1:
                return root, PatchApplyResult(
                    "BLOCKED",
                    "PATCH_OLD_TEXT_NOT_UNIQUE",
                    f"预期旧文本在 {change.path} 中匹配 {matches} 次，要求恰好一次",
                    failed_path=change.path,
                )
            updated = original_text.replace(expected_old_text, new_text, 1).encode("utf-8")
            total_bytes += len(updated)
            if total_bytes > MAX_PATCH_BYTES:
                return root, PatchApplyResult("BLOCKED", "PATCH_TOTAL_TOO_LARGE", "结构化补丁总量超过限制")
            prepared.append(
                _PreparedPatchChange(
                    target=target,
                    relative_path=target.relative_to(root).as_posix(),
                    original=original_text.encode("utf-8"),
                    updated=updated,
                )
            )
        return root, prepared

    def repair_snapshot(
        self,
        workspace_root: Path,
        path: str,
        permission: PermissionGrant,
        candidate_files: set[str] | None = None,
    ) -> str | None:
        """返回一次补丁纠错所需的受限文件快照，不记录到审计或任务状态。"""
        if candidate_files is not None and path not in candidate_files:
            return None
        root = workspace_root.expanduser().resolve()
        target = (root / path).resolve()
        decision = PolicyGuard(root, permission).check_path(ToolName.APPLY_PATCH, target)
        if not decision.allowed or not target.is_file():
            return None
        try:
            if target.stat().st_size > MAX_PATCH_REPAIR_SNAPSHOT_BYTES:
                return None
            raw = target.read_bytes()
        except OSError:
            return None
        if b"\0" in raw:
            return None
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None


class VerificationRunner:
    def __init__(self, maven_runner: MavenRecipeRunner | None = None, gradle_runner: GradleRecipeRunner | None = None, pytest_runner: PytestRecipeRunner | None = None, node_runner: NodeRecipeRunner | None = None) -> None:
        self._maven_runner = maven_runner or MavenRecipeRunner()
        self._gradle_runner = gradle_runner or GradleRecipeRunner()
        self._pytest_runner = pytest_runner or PytestRecipeRunner()
        self._node_runner = node_runner or NodeRecipeRunner()

    def run(
        self,
        workspace_root: Path,
        proposal: PatchProposal,
        permission: PermissionGrant,
        cancellation_requested: Callable[[], bool] | None = None,
    ) -> VerificationResult:
        if isinstance(proposal.recipe, GradleRecipeName):
            return VerificationResult(
                self._gradle_runner.run(
                    workspace_root,
                    proposal.recipe,
                    permission,
                    proposal.test_class,
                    cancellation_requested=cancellation_requested,
                )
            )
        if isinstance(proposal.recipe, PytestRecipeName):
            return VerificationResult(
                self._pytest_runner.run(
                    workspace_root,
                    proposal.recipe,
                    permission,
                    proposal.test_class,
                    cancellation_requested=cancellation_requested,
                )
            )
        if isinstance(proposal.recipe, NodeRecipeName):
            return VerificationResult(
                self._node_runner.run(
                    workspace_root,
                    proposal.recipe,
                    permission,
                    proposal.test_class,
                    cancellation_requested=cancellation_requested,
                )
            )
        if cancellation_requested is None:
            return VerificationResult(
                self._maven_runner.run(workspace_root, proposal.recipe, permission, proposal.test_class)
            )
        return VerificationResult(
            self._maven_runner.run(
                workspace_root,
                proposal.recipe,
                permission,
                proposal.test_class,
                cancellation_requested=cancellation_requested,
            )
        )


def _adapt_line_endings(value: str, target_text: str) -> str:
    """仅在目标文件为 CRLF、模型文本为 LF 时适配，避免跨平台补丁误判。"""
    if "\r\n" in target_text and "\r\n" not in value:
        return value.replace("\n", "\r\n")
    return value
