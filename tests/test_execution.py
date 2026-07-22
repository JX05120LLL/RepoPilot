from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from repopilot_guard.execution import PatchFileChange, PatchProposal, StructuredPatchApplier
from repopilot_guard.models import TaskMode, WorkspaceMode
from repopilot_guard.permissions import PermissionGrant
from repopilot_guard.policy import MavenRecipeName
from repopilot_guard.recipes import MavenRecipeRunner, RecipeCommand


class StructuredPatchApplierTests(unittest.TestCase):
    def test_validates_every_change_before_writing_any_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._init_repository(root)
            first = root / "First.java"
            second = root / "Second.java"
            first.write_text("class First { String value = \"old\"; }\n", encoding="utf-8")
            second.write_text("class Second { }\n", encoding="utf-8")
            proposal = PatchProposal(
                summary="测试原子校验",
                changes=[
                    PatchFileChange(path="First.java", expected_old_text="old", new_text="new"),
                    PatchFileChange(path="Second.java", expected_old_text="missing", new_text="new"),
                ],
            )

            result = StructuredPatchApplier().apply(root, proposal, PermissionGrant.safe(), {"First.java", "Second.java"})

            self.assertEqual("BLOCKED", result.status)
            self.assertEqual("PATCH_OLD_TEXT_NOT_UNIQUE", result.code)
            self.assertIn("old", first.read_text(encoding="utf-8"))

    def test_blocks_sensitive_path_in_safe_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._init_repository(root)
            (root / ".env").write_text("KEY=old\n", encoding="utf-8")
            proposal = PatchProposal(summary="错误目标", changes=[PatchFileChange(path=".env", expected_old_text="old", new_text="new")])

            result = StructuredPatchApplier().apply(root, proposal, PermissionGrant.safe(), {".env"})

            self.assertEqual("BLOCKED", result.status)
            self.assertEqual("PROTECTED_FILE_BLOCKED", result.code)

    def test_accepts_lf_model_text_for_a_crlf_repository_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._init_repository(root)
            source = root / "Sample.java"
            source.write_bytes(b"class Sample {\r\n  String value = \"old\";\r\n}\r\n")
            proposal = PatchProposal(
                summary="跨平台换行补丁",
                changes=[PatchFileChange(path="Sample.java", expected_old_text='String value = "old";\n', new_text='String value = "new";\n')],
            )

            result = StructuredPatchApplier().apply(root, proposal, PermissionGrant.safe(), {"Sample.java"})

            self.assertEqual("READY", result.status)
            self.assertEqual(b"class Sample {\r\n  String value = \"new\";\r\n}\r\n", source.read_bytes())

    @staticmethod
    def _init_repository(root: Path) -> None:
        subprocess.run(("git", "-C", str(root), "init", "-b", "main"), check=True, capture_output=True)
        subprocess.run(("git", "-C", str(root), "config", "user.name", "RepoPilot Test"), check=True, capture_output=True)
        subprocess.run(("git", "-C", str(root), "config", "user.email", "test@example.invalid"), check=True, capture_output=True)
        (root / "pom.xml").write_text("<project />\n", encoding="utf-8")
        subprocess.run(("git", "-C", str(root), "add", "."), check=True, capture_output=True)
        subprocess.run(("git", "-C", str(root), "commit", "-m", "fixture"), check=True, capture_output=True)


class TaskModeTests(unittest.TestCase):
    def test_product_modes_are_fixed_workspace_permission_pairs(self) -> None:
        self.assertEqual(WorkspaceMode.WORKTREE, TaskMode.SAFE_ISOLATED.workspace_mode)
        self.assertEqual("safe", TaskMode.SAFE_ISOLATED.permission_mode)
        self.assertEqual(WorkspaceMode.LOCAL, TaskMode.FULL_LOCAL.workspace_mode)
        self.assertEqual("full", TaskMode.FULL_LOCAL.permission_mode)


class MavenCancellationTests(unittest.TestCase):
    def test_cancellation_terminates_only_the_maven_process_started_by_repopilot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            runner = MavenRecipeRunner(_SleepingRecipeCatalog(root), timeout_seconds=10)
            started = time.monotonic()

            result = runner.run(root, MavenRecipeName.TEST, PermissionGrant.safe(), cancellation_requested=lambda: True)

            self.assertEqual("BLOCKED", result.status)
            self.assertEqual("MAVEN_CANCELLED", result.code)
            self.assertLess(time.monotonic() - started, 3)


class _SleepingRecipeCatalog:
    """避免依赖本机 Maven，用 Python 子进程模拟一个可被终止的构建。"""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    def build(
        self,
        _repository: Path,
        recipe: MavenRecipeName,
        _permission: PermissionGrant,
        _test_class: str | None,
    ) -> RecipeCommand:
        return RecipeCommand(recipe, (sys.executable, "-c", "import time; time.sleep(30)"), self._workspace)
