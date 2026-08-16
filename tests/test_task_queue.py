"""阶段 2（2.2）后台任务：有界并发队列。"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from repopilot_guard.api import create_app
from repopilot_guard.config import AppSettings
from repopilot_guard.permissions import FULL_ACCESS_CONFIRMATION
from repopilot_guard.project_registry import ProjectRegistry
from repopilot_guard.task_store import TaskStore


class _CountingRunner:
    def __init__(self) -> None:
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self._lock = Lock()

    def run(self, request: object, thread_id: str, permission: object) -> object:
        with self._lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.05)
        with self._lock:
            self.active -= 1
        return SimpleNamespace(
            to_dict=lambda: {
                "thread_id": thread_id,
                "task_id": getattr(request, "task_id", "task-1"),
                "status": "REPORT",
                "pending_approval": False,
                "verdict": "UNVERIFIED",
                "state": {"tool_events": []},
            }
        )

    def get(self, thread_id: str) -> object:
        # 测试替身不维护 checkpoint；让 _task_snapshot 回退到持久化任务记录。
        raise ValueError("NOT_FOUND")


class TaskQueueTests(unittest.TestCase):
    def test_max_concurrent_tasks_config_default_and_override(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(2, AppSettings().max_concurrent_tasks)
        with patch.dict(os.environ, {"REPOPILOT_MAX_CONCURRENT_TASKS": "3"}, clear=True):
            self.assertEqual(3, AppSettings().max_concurrent_tasks)
        with patch.dict(os.environ, {"REPOPILOT_MAX_CONCURRENT_TASKS": "0"}, clear=True):
            with self.assertRaises(Exception):
                AppSettings()

    def test_bounded_queue_limits_concurrent_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "plain-project"
            repository.mkdir()
            registry = ProjectRegistry(root / "state.sqlite")
            store = TaskStore(root / "state.sqlite")
            project = registry.add(repository, "队列项目")
            runner = _CountingRunner()
            try:
                with TestClient(create_app(runner, registry, root / "runs", task_store=store, max_concurrent_tasks=1)) as client:
                    for index in range(3):
                        response = client.post(
                            "/api/tasks",
                            json={
                                "project_id": project.project_id,
                                "description": f"直接修改代码 {index}",
                                "task_mode": "full-local",
                                "operation": "change",
                                "confirmation": FULL_ACCESS_CONFIRMATION,
                            },
                        )
                        self.assertEqual(200, response.status_code)

                    deadline = time.monotonic() + 5
                    while store.outcome_summary()["terminal"] < 3 and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertEqual(3, store.outcome_summary()["terminal"])

                self.assertEqual(3, runner.calls)
                self.assertEqual(1, runner.max_active)
            finally:
                registry.close()
                store.close()


if __name__ == "__main__":
    unittest.main()
