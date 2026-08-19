from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from repopilot_guard.coordinator import TaskCoordinator
from repopilot_guard.models import TaskRequest, TaskState, TaskVerdict


class TaskCoordinatorTests(unittest.TestCase):
    def test_valid_java_repository_is_unverified_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repository"
            repository.mkdir()
            (repository / ".git").mkdir()
            (repository / "pom.xml").write_text("<project />", encoding="utf-8")
            (repository / "src" / "main" / "java").mkdir(parents=True)

            result = TaskCoordinator().run_dry_run(
                TaskRequest(repository=repository, description="修复订单查询边界条件", output_root=root / "runs")
            )

            self.assertEqual(TaskVerdict.UNVERIFIED, result.verdict)
            self.assertEqual(TaskState.REPORT, result.final_state)
            self.assertTrue(result.events_path.is_file())
            self.assertTrue(result.report_path.is_file())
            events = [json.loads(line) for line in result.events_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual("task_created", events[0]["event_type"])
            self.assertEqual("task_completed", events[-1]["event_type"])

    def test_missing_git_repository_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repository"
            repository.mkdir()
            (repository / "pom.xml").write_text("<project />", encoding="utf-8")

            result = TaskCoordinator().run_dry_run(
                TaskRequest(repository=repository, description="修复订单查询边界条件", output_root=root / "runs")
            )

            self.assertEqual(TaskVerdict.BLOCKED, result.verdict)
            self.assertEqual(TaskState.BLOCKED, result.final_state)
            self.assertIn("Repository is not a Git working tree.", result.preflight.errors)
