from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from repopilot_guard.policy import MavenRecipeName, PolicyGuard, ToolName
from repopilot_guard.recipes import MavenRecipeCatalog


class PolicyGuardTests(unittest.TestCase):
    def test_blocks_paths_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory) / "workspace"
            workspace.mkdir()
            decision = PolicyGuard(workspace).check_path(ToolName.READ_FILE, workspace.parent / "outside.txt")

        self.assertFalse(decision.allowed)
        self.assertIn("escapes", decision.reason)

    def test_blocks_sensitive_production_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            decision = PolicyGuard(workspace).check_path(
                ToolName.APPLY_PATCH,
                workspace / "src" / "main" / "resources" / "application-prod.yml",
            )

        self.assertFalse(decision.allowed)
        self.assertIn("Production", decision.reason)

    def test_rejects_injected_targeted_test(self) -> None:
        guard = PolicyGuard(Path.cwd())
        decision = guard.check_recipe(MavenRecipeName.TARGETED_TEST, "OrderTest; rm -rf /")

        self.assertFalse(decision.allowed)

    def test_builds_allowlisted_targeted_test_recipe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory)
            command = MavenRecipeCatalog().build(
                repository,
                MavenRecipeName.TARGETED_TEST,
                "com.example.OrderServiceTest",
            )

        self.assertEqual("targeted_test", command.recipe.value)
        self.assertIn("-Dtest=com.example.OrderServiceTest", command.argv)
        self.assertNotIn(";", " ".join(command.argv))

