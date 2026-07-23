from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from repopilot_guard.task_store import TaskStore


class TaskStoreTests(unittest.TestCase):
    @staticmethod
    def _create_task(store: TaskStore, root: Path, thread_id: str = "thread-lease") -> None:
        store.create(
            thread_id=thread_id,
            task_id=f"task-{thread_id}",
            project_id="project-1",
            repository=root / "repo",
            output_root=root / "runs",
            task_mode="safe-isolated",
            permission_mode="safe",
            workspace_mode="worktree",
        )

    def test_rejects_task_id_that_could_escape_artifact_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = TaskStore(Path(temporary_directory) / "state.sqlite")
            try:
                with self.assertRaisesRegex(ValueError, "INVALID_TASK_ID"):
                    store.create(
                        thread_id="thread-invalid",
                        task_id="../outside",
                        project_id=None,
                        repository=Path(temporary_directory) / "repo",
                        output_root=Path(temporary_directory) / "runs",
                        task_mode="safe-isolated",
                        permission_mode="safe",
                        workspace_mode="worktree",
                    )
            finally:
                store.close()

    def test_persists_task_and_replays_only_new_events_after_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "state.sqlite"
            store = TaskStore(database_path)
            try:
                task = store.create(
                    thread_id="thread-1",
                    task_id="task-1",
                    project_id="project-1",
                    repository=Path(temporary_directory) / "repo",
                    output_root=Path(temporary_directory) / "runs",
                    task_mode="safe-isolated",
                    permission_mode="safe",
                    workspace_mode="worktree",
                )
                self.assertEqual("RUNNING", task.status)
                self.assertTrue(task.trace_id.startswith("trace-"))
                store.sync_graph_result(
                    {
                        "thread_id": "thread-1",
                        "status": "WAITING_APPROVAL",
                        "pending_approval": True,
                        "verdict": None,
                        "state": {
                            "error_summary": None,
                            "tool_events": [{"type": "FILE_READ", "arguments": {"path": "pom.xml", "api_key": "hidden"}}],
                        },
                    }
                )
                events = store.events_after("thread-1", 0)
                self.assertGreaterEqual(len(events), 3)
                self.assertEqual("TASK_CREATED", events[0].event_type)
                self.assertEqual({task.trace_id}, {event.trace_id for event in events})
                self.assertEqual("TASK_STATE", events[1].event_type)
                self.assertEqual("[REDACTED]", events[2].payload["arguments"]["api_key"])
                last_sequence = events[-1].sequence
            finally:
                store.close()

            reopened = TaskStore(database_path)
            try:
                task = reopened.get("thread-1")
                self.assertEqual("WAITING_APPROVAL", task.status)
                self.assertTrue(task.pending_approval)
                self.assertEqual((), reopened.events_after("thread-1", last_sequence))
                self.assertEqual(1, len(reopened.list()))
            finally:
                reopened.close()

    def test_task_operation_persists_and_invalid_checkpoint_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database_path = root / "state.sqlite"
            store = TaskStore(database_path)
            try:
                task = store.create(
                    thread_id="thread-research",
                    task_id="task-research",
                    project_id="project-1",
                    repository=root / "repo",
                    output_root=root / "runs",
                    task_mode="safe-isolated",
                    task_operation="research",
                    permission_mode="safe",
                    workspace_mode="worktree",
                )
                self.assertEqual("research", task.task_operation)
                self.assertEqual("research", task.to_dict()["task_operation"])
                self.assertEqual("research", store.events_after(task.thread_id, 0)[0].payload["task_operation"])
                with self.assertRaisesRegex(ValueError, "TASK_OPERATION_INVALID"):
                    store.sync_graph_result(
                        {
                            "thread_id": task.thread_id,
                            "status": "WAITING_APPROVAL",
                            "state": {"task_operation": "unknown", "tool_events": []},
                        }
                    )
            finally:
                store.close()

            reopened = TaskStore(database_path)
            try:
                self.assertEqual("research", reopened.get("thread-research").task_operation)
                self.assertEqual("research", reopened.list()[0].task_operation)
            finally:
                reopened.close()

    def test_legacy_task_table_migrates_operation_without_losing_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "legacy.sqlite"
            connection = sqlite3.connect(database_path)
            try:
                connection.executescript(
                    """
                    CREATE TABLE tasks (
                        thread_id TEXT PRIMARY KEY,
                        trace_id TEXT NOT NULL,
                        task_id TEXT NOT NULL UNIQUE,
                        display_title TEXT,
                        project_id TEXT,
                        repository TEXT NOT NULL,
                        output_root TEXT NOT NULL,
                        task_mode TEXT NOT NULL,
                        permission_mode TEXT NOT NULL,
                        workspace_mode TEXT NOT NULL,
                        status TEXT NOT NULL,
                        pending_approval INTEGER NOT NULL,
                        verdict TEXT,
                        error_summary TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        heartbeat_at TEXT NOT NULL,
                        lease_expires_at TEXT,
                        cancellation_requested_at TEXT,
                        cancellation_reason TEXT,
                        archived_at TEXT
                    );
                    """
                )
                timestamp = "2026-01-01T00:00:00+00:00"
                connection.execute(
                    """
                    INSERT INTO tasks(
                        thread_id, trace_id, task_id, display_title, project_id, repository, output_root,
                        task_mode, permission_mode, workspace_mode, status, pending_approval,
                        created_at, updated_at, heartbeat_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "legacy-thread",
                        "trace-legacy",
                        "legacy-task",
                        "旧任务",
                        "project-1",
                        str(Path(temporary_directory) / "repo"),
                        str(Path(temporary_directory) / "runs"),
                        "safe-isolated",
                        "safe",
                        "worktree",
                        "BLOCKED",
                        0,
                        timestamp,
                        timestamp,
                        timestamp,
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            migrated = TaskStore(database_path)
            try:
                task = migrated.get("legacy-thread")
                self.assertEqual("change", task.task_operation)
                self.assertEqual("旧任务", task.display_title)
            finally:
                migrated.close()

            connection = sqlite3.connect(database_path)
            try:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
                self.assertIn("task_operation", columns)
            finally:
                connection.close()

    def test_task_display_title_is_bounded_and_redacts_inline_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = TaskStore(root / "state.sqlite")
            try:
                task = store.create(
                    thread_id="thread-title",
                    task_id="task-title",
                    display_title=(
                        "  修复订单权限校验  API_KEY=do-not-expose  "
                        + "并补充回归测试" * 20
                    ),
                    project_id="project-1",
                    repository=root / "repo",
                    output_root=root / "runs",
                    task_mode="safe-isolated",
                    permission_mode="safe",
                    workspace_mode="worktree",
                )
                self.assertLessEqual(len(task.display_title or ""), 80)
                self.assertIn("API_KEY=[REDACTED]", task.display_title or "")
                self.assertNotIn("do-not-expose", task.display_title or "")
                self.assertEqual(task.display_title, store.get("thread-title").display_title)
            finally:
                store.close()

    def test_legacy_empty_trace_id_is_backfilled_once_and_remains_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database_path = root / "state.sqlite"
            store = TaskStore(database_path)
            try:
                self._create_task(store, root, "thread-trace-migration")
            finally:
                store.close()

            connection = sqlite3.connect(database_path)
            try:
                connection.execute("UPDATE tasks SET trace_id = '' WHERE thread_id = ?", ("thread-trace-migration",))
                connection.commit()
            finally:
                connection.close()

            reopened = TaskStore(database_path)
            try:
                first = reopened.get("thread-trace-migration")
                self.assertTrue(first.trace_id.startswith("trace-"))
                self.assertEqual({first.trace_id}, {event.trace_id for event in reopened.events_after(first.thread_id, 0)})
            finally:
                reopened.close()

            stable = TaskStore(database_path)
            try:
                self.assertEqual(first.trace_id, stable.get("thread-trace-migration").trace_id)
            finally:
                stable.close()

    def test_recovers_task_index_from_existing_graph_checkpoint_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = TaskStore(Path(temporary_directory) / "state.sqlite")
            try:
                recovered = store.sync_graph_result(
                    {
                        "thread_id": "legacy-thread",
                        "task_id": "legacy-task",
                        "status": "WAITING_APPROVAL",
                        "pending_approval": True,
                        "verdict": None,
                        "state": {
                            "project_id": "project-legacy",
                            "repository": str(Path(temporary_directory) / "repo"),
                            "output_root": str(Path(temporary_directory) / "runs"),
                            "permission_mode": "safe",
                            "workspace_mode": "worktree",
                            "error_summary": None,
                            "tool_events": [],
                        },
                    }
                )
                self.assertEqual("legacy-task", recovered.task_id)
                self.assertEqual("safe-isolated", recovered.task_mode)
                self.assertTrue(recovered.pending_approval)
            finally:
                store.close()

    def test_materializes_hashed_artifacts_from_graph_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = TaskStore(root / "state.sqlite")
            try:
                store.create(
                    thread_id="thread-artifacts",
                    task_id="task-artifacts",
                    project_id="project-1",
                    repository=root / "repo",
                    output_root=root / "runs",
                    task_mode="safe-isolated",
                    permission_mode="safe",
                    workspace_mode="worktree",
                )
                store.sync_graph_result(
                    {
                        "thread_id": "thread-artifacts",
                        "status": "REPORT",
                        "pending_approval": False,
                        "verdict": "PASSED",
                        "state": {
                            "status": "REPORT",
                            "tool_events": [],
                            "plan": {"summary": "修复空租户参数", "candidate_files": ["src/App.java"], "steps": ["增加校验"]},
                            "patch_proposal": {"changes": [{"path": "src/App.java"}]},
                            "verification_result": {"status": "PASSED", "recipe": "test"},
                            "git_diff": "diff --git a/src/App.java b/src/App.java\n",
                            "error_summary": None,
                        },
                    }
                )
                artifacts = {item.kind: item for item in store.artifacts("thread-artifacts")}
                self.assertEqual(
                    {"plan_json", "plan_markdown", "patch_proposal", "verification", "git_diff", "telemetry", "report"},
                    set(artifacts),
                )
                self.assertEqual(64, len(artifacts["git_diff"].sha256))
                artifact, report = store.read_artifact("thread-artifacts", "report")
                self.assertEqual("report", artifact.kind)
                self.assertIn("真实 Diff 与成功验证证据", report)
                self.assertTrue((root / "runs" / "task-artifacts" / "changes.diff").is_file())
            finally:
                store.close()

    def test_telemetry_aggregates_persisted_node_and_model_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = TaskStore(root / "state.sqlite")
            try:
                self._create_task(store, root, "thread-telemetry")
                store.sync_graph_result(
                    {
                        "thread_id": "thread-telemetry",
                        "status": "WAITING_APPROVAL",
                        "pending_approval": True,
                        "verdict": None,
                        "state": {
                            "tool_events": [
                                {"type": "TASK_BUDGET_SNAPSHOT", "configured": True, "max_total_tokens": 50, "max_estimated_cost": 0.0001, "currency": "CNY"},
                                {"type": "NODE_COMPLETED", "node": "ANALYZE", "duration_ms": 17},
                                {"type": "MODEL_USAGE", "reported": True, "input_tokens": 11, "output_tokens": 7, "total_tokens": 18, "estimated_cost": 0.00005, "currency": "CNY"},
                                {"type": "MODEL_USAGE", "reported": False},
                                {"type": "GRAPH_BLOCKED", "code": "MODEL_TOKEN_BUDGET_EXCEEDED"},
                            ],
                            "error_summary": None,
                        },
                    }
                )

                telemetry = store.telemetry("thread-telemetry")

                self.assertEqual(1, telemetry["node_count"])
                self.assertEqual(17, telemetry["node_total_duration_ms"])
                self.assertEqual(11, telemetry["model"]["input_tokens"])
                self.assertEqual(1, telemetry["model"]["unavailable_operations"])
                self.assertEqual(0.00005, telemetry["model"]["estimated_cost"])
                self.assertEqual("BLOCKED", telemetry["budget"]["status"])
                self.assertEqual("MODEL_TOKEN_BUDGET_EXCEEDED", telemetry["budget"]["code"])
                artifact, content = store.read_artifact("thread-telemetry", "telemetry")
                self.assertEqual("telemetry", artifact.kind)
                self.assertIn('"node_total_duration_ms": 17', content)
            finally:
                store.close()

    def test_artifact_history_is_immutable_and_repeated_projection_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = TaskStore(root / "state.sqlite")
            try:
                self._create_task(store, root, "thread-history")
                first = {
                    "thread_id": "thread-history",
                    "status": "WAITING_APPROVAL",
                    "pending_approval": True,
                    "verdict": None,
                    "state": {
                        "tool_events": [],
                        "plan": {"summary": "第一版计划", "candidate_files": ["src/App.java"]},
                        "error_summary": None,
                    },
                }
                second = {
                    **first,
                    "state": {
                        "tool_events": [],
                        "plan": {"summary": "第二版计划", "candidate_files": ["src/App.java"]},
                        "error_summary": None,
                    },
                }

                store.sync_graph_result(first)
                store.sync_graph_result(second)
                store.sync_graph_result(second)

                versions = store.artifact_versions("thread-history", "plan_json")
                self.assertEqual([2, 1], [item.version for item in versions])
                self.assertTrue(all(item.relative_path.startswith("history/plan_json/") for item in versions))
                version_one, first_content = store.read_artifact_version("thread-history", "plan_json", 1)
                version_two, second_content = store.read_artifact_version("thread-history", "plan_json", 2)
                current, current_content = store.read_artifact("thread-history", "plan_json")
                self.assertNotEqual(version_one.sha256, version_two.sha256)
                self.assertIn("第一版计划", first_content)
                self.assertIn("第二版计划", second_content)
                self.assertEqual(version_two.sha256, current.sha256)
                self.assertEqual(second_content, current_content)

                historic_path = root / "runs" / "task-thread-history" / version_one.relative_path
                historic_path.write_text("已被篡改", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "TASK_ARTIFACT_INTEGRITY_MISMATCH"):
                    store.read_artifact_version("thread-history", "plan_json", 1)
            finally:
                store.close()

    def test_cancellation_request_survives_old_checkpoint_until_worker_exits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = TaskStore(root / "state.sqlite")
            try:
                self._create_task(store, root)
                running = store.begin_execution("thread-lease", lease_seconds=60)
                self.assertEqual("RUNNING", running.status)
                requested = store.request_cancellation("thread-lease", "停止本次研究")
                self.assertEqual("CANCELLATION_REQUESTED", requested.status)
                projected = store.sync_graph_result(
                    {
                        "thread_id": "thread-lease",
                        "status": "WAITING_APPROVAL",
                        "pending_approval": True,
                        "verdict": None,
                        "state": {"tool_events": [], "error_summary": None},
                    },
                    execution_finished=True,
                )
                self.assertEqual("CANCELLATION_REQUESTED", projected.status)
                cancelled = store.complete_cancellation("thread-lease")
                self.assertEqual("CANCELLED", cancelled.status)
                self.assertEqual("CANCELLED", cancelled.verdict)
                self.assertIsNone(cancelled.lease_expires_at)
                self.assertIn("TASK_CANCELLED", [event.event_type for event in store.events_after("thread-lease", 0)])
            finally:
                store.close()

    def test_reaps_expired_lease_and_archives_without_deleting_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = TaskStore(root / "state.sqlite")
            try:
                self._create_task(store, root, "thread-expired")
                store.begin_execution("thread-expired", lease_seconds=1)
                recovered = store.reap_expired_leases(now="9999-01-01T00:00:00+00:00")
                self.assertEqual(["thread-expired"], [item.thread_id for item in recovered])
                blocked = store.get("thread-expired")
                self.assertEqual("BLOCKED", blocked.status)
                self.assertEqual("TASK_LEASE_EXPIRED", blocked.error_summary)
                archived = store.archive("thread-expired")
                self.assertIsNotNone(archived.archived_at)
                self.assertEqual((), store.list())
                self.assertEqual((archived,), store.list(include_archived=True))
                self.assertIn("TASK_ARCHIVED", [event.event_type for event in store.events_after("thread-expired", 0)])
            finally:
                store.close()
