from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from repopilot_guard.execution import PatchFileChange, PatchProposal, StructuredPatchApplier
from repopilot_guard.models import TaskMode, WorkspaceMode
from repopilot_guard.permissions import FULL_ACCESS_CONFIRMATION, PermissionGrant, PermissionMode
from repopilot_guard.policy import GradleRecipeName, MavenRecipeName, NodeRecipeName, PytestRecipeName
from repopilot_guard.recipes import GradleRecipeCatalog, GradleRecipeRunner, MavenRecipeRunner, NodeRecipeCatalog, NodeRecipeRunner, PytestRecipeCatalog, PytestRecipeRunner, RecipeCommand


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

    def test_preview_uses_the_same_validation_without_writing_the_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._init_repository(root)
            source = root / "Preview.java"
            source.write_text("class Preview { String value = \"old\"; }\n", encoding="utf-8")
            proposal = PatchProposal(
                summary="预览结构化替换",
                changes=[PatchFileChange(path="Preview.java", expected_old_text="old", new_text="new")],
            )

            result = StructuredPatchApplier().preview(root, proposal, PermissionGrant.safe(), {"Preview.java"})

            self.assertEqual("READY", result.status)
            self.assertEqual("PATCH_PREVIEW_READY", result.code)
            self.assertIn("-class Preview { String value = \"old\"; }", result.diff)
            self.assertIn("+class Preview { String value = \"new\"; }", result.diff)
            self.assertIn("old", source.read_text(encoding="utf-8"))

    def test_non_git_workspace_uses_unified_diff_after_applying_patch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "Sample.java"
            source.write_text("class Sample { String value = \"old\"; }\n", encoding="utf-8")
            proposal = PatchProposal(
                summary="替换演示值",
                changes=[PatchFileChange(path="Sample.java", expected_old_text='String value = "old";', new_text='String value = "new";')],
            )

            result = StructuredPatchApplier().apply(root, proposal, PermissionGrant(PermissionMode.FULL, FULL_ACCESS_CONFIRMATION), {"Sample.java"})

        self.assertEqual("READY", result.status)
        self.assertIn("--- a/Sample.java", result.diff)
        self.assertIn('+class Sample { String value = "new"; }', result.diff)

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


class GradleRecipeTests(unittest.TestCase):
    def test_gradle_catalog_uses_fixed_direct_argv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "build.gradle.kts").write_text("plugins { java }\n", encoding="utf-8")

            test_command = GradleRecipeCatalog().build(root, GradleRecipeName.TEST, permission=PermissionGrant.safe())
            targeted_command = GradleRecipeCatalog().build(
                root,
                GradleRecipeName.TARGETED_TEST,
                "com.example.OrderServiceTest",
                PermissionGrant.safe(),
            )

        self.assertEqual("gradle", Path(test_command.argv[0]).stem.lower())
        self.assertEqual(("--no-daemon", "--console=plain", "-q", "test"), test_command.argv[1:])
        self.assertEqual(("test", "--tests", "com.example.OrderServiceTest"), targeted_command.argv[-3:])

    def test_gradle_rejects_targeted_recipe_without_a_safe_test_class(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with self.assertRaises(ValueError):
                GradleRecipeCatalog().build(root, GradleRecipeName.TARGETED_TEST, "../unsafe", PermissionGrant.safe())

    def test_gradle_runner_reports_real_process_failure_without_claiming_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            result = GradleRecipeRunner(_FailingGradleCatalog(root)).run(root, GradleRecipeName.TEST, PermissionGrant.safe())

        self.assertEqual("FAILED", result.status)
        self.assertEqual("GRADLE_FAILED", result.code)
        self.assertEqual(7, result.exit_code)


class PytestRecipeTests(unittest.TestCase):
    def test_pytest_catalog_uses_fixed_module_argv_and_one_validated_selector(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            command = PytestRecipeCatalog().build(
                root,
                PytestRecipeName.TARGETED_TEST,
                "tests/test_orders.py::test_create_order",
                PermissionGrant.safe(),
            )

        self.assertEqual(sys.executable, command.argv[0])
        self.assertEqual(("-m", "pytest", "-q", "tests/test_orders.py::test_create_order"), command.argv[1:])

    def test_pytest_rejects_flag_or_path_escape_selector(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for selector in ("--collect-only", "../outside.py::test_x"):
                with self.assertRaises(ValueError):
                    PytestRecipeCatalog().build(root, PytestRecipeName.TARGETED_TEST, selector, PermissionGrant.safe())

    def test_pytest_runner_reports_real_process_failure_without_claiming_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            result = PytestRecipeRunner(_FailingPytestCatalog(root)).run(root, PytestRecipeName.TEST, PermissionGrant.safe())

        self.assertEqual("FAILED", result.status)
        self.assertEqual("PYTEST_FAILED", result.code)
        self.assertEqual(9, result.exit_code)


class NodeRecipeTests(unittest.TestCase):
    def test_npm_catalog_uses_only_fixed_test_script_argv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "package.json").write_text('{"scripts":{"test":"node --test"}}\n', encoding="utf-8")
            command = NodeRecipeCatalog().build(root, NodeRecipeName.NPM_TEST, permission=PermissionGrant.safe())

        self.assertEqual("npm", Path(command.argv[0]).stem.lower())
        self.assertEqual(("run", "--silent", "test"), command.argv[1:])

    def test_pnpm_catalog_prefers_fixed_corepack_argv_without_npm_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "package.json").write_text('{"scripts":{"test":"node --test"}}\n', encoding="utf-8")
            catalog = NodeRecipeCatalog(
                command_lookup=lambda command: "C:\\tools\\corepack.cmd" if command == "corepack.cmd" else None,
            )
            command = catalog.build(root, NodeRecipeName.PNPM_TEST, permission=PermissionGrant.safe())

        self.assertEqual(("C:\\tools\\corepack.cmd", "pnpm", "run", "--silent", "test"), command.argv)

    def test_node_catalog_blocks_missing_test_script_and_extra_selector(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "package.json").write_text('{"scripts":{}}\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                NodeRecipeCatalog().build(root, NodeRecipeName.NPM_TEST, permission=PermissionGrant.safe())
            (root / "package.json").write_text('{"scripts":{"test":"node --test"}}\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                NodeRecipeCatalog().build(root, NodeRecipeName.NPM_TEST, "--injected", PermissionGrant.safe())

    def test_node_runner_reports_real_process_failure_without_claiming_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            result = NodeRecipeRunner(_FailingNodeCatalog(root)).run(root, NodeRecipeName.NPM_TEST, PermissionGrant.safe())

        self.assertEqual("FAILED", result.status)
        self.assertEqual("NODE_TEST_FAILED", result.code)
        self.assertEqual(11, result.exit_code)


class _FailingGradleCatalog:
    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    def build(
        self,
        _repository: Path,
        recipe: GradleRecipeName,
        _test_class: str | None,
        _permission: PermissionGrant,
    ) -> RecipeCommand:
        return RecipeCommand(recipe, (sys.executable, "-c", "import sys; sys.exit(7)"), self._workspace)


class _FailingPytestCatalog:
    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    def build(
        self,
        _repository: Path,
        recipe: PytestRecipeName,
        _test_class: str | None,
        _permission: PermissionGrant,
    ) -> RecipeCommand:
        return RecipeCommand(recipe, (sys.executable, "-c", "import sys; sys.exit(9)"), self._workspace)


class _FailingNodeCatalog:
    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    def build(
        self,
        _repository: Path,
        recipe: NodeRecipeName,
        _test_class: str | None,
        _permission: PermissionGrant,
    ) -> RecipeCommand:
        return RecipeCommand(recipe, (sys.executable, "-c", "import sys; sys.exit(11)"), self._workspace)


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
