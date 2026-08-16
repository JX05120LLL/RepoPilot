from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

from repopilot_guard.task_store import TaskStore


class TaskStoreTests(unittest.TestCase):
    def test_registers_hashed_mcp_output_artifact_from_metadata_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = TaskStore(root / "state.sqlite")
            try:
                self._create_task(store, root, "thread-mcp-output")
                content = ("x" * 30_000 + "仅产物可见的尾部内容").encode("utf-8")
                digest = sha256(content).hexdigest()
                relative_path = f"mcp/outputs/{digest}.json"
                source = root / "runs" / "task-thread-mcp-output" / relative_path
                source.parent.mkdir(parents=True)
                source.write_bytes(content)
                event = {
                    "type": "TOOL_CALL",
                    "name": "mcp__docs__search",
                    "status": "READY",
                    "code": "MCP_TOOL_COMPLETED",
                    "artifact": {
                        "status": "READY",
                        "kind": "mcp_tool_output",
                        "relative_path": relative_path,
                        "sha256": digest,
                        "output_sha256": digest,
                        "size_bytes": len(content),
                    },
                }
                store.sync_graph_result(
                    {
                        "thread_id": "thread-mcp-output",
                        "status": "REPORT",
                        "pending_approval": False,
                        "verdict": "UNVERIFIED",
                        "state": {"tool_events": [event]},
                    }
                )

                kind = f"mcp_output_{digest[:16]}"
                artifact, saved = store.read_artifact("thread-mcp-output", kind)
                self.assertEqual(relative_path, artifact.relative_path)
                self.assertEqual(content.decode("utf-8"), saved)
                persisted_events = store.events_after("thread-mcp-output", 0)
                self.assertNotIn("仅产物可见的尾部内容", json.dumps([event.payload for event in persisted_events], ensure_ascii=False))
                self.assertNotIn("artifact", next(item for item in persisted_events if item.event_type == "TOOL_CALL").to_public_dict()["payload"])
            finally:
                store.close()

    def test_rejects_tampered_mcp_output_artifact_before_registration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = TaskStore(root / "state.sqlite")
            try:
                self._create_task(store, root, "thread-mcp-tampered")
                content = b"original output"
                digest = sha256(content).hexdigest()
                relative_path = f"mcp/outputs/{digest}.json"
                source = root / "runs" / "task-thread-mcp-tampered" / relative_path
                source.parent.mkdir(parents=True)
                source.write_bytes(b"tampered output")
                store.sync_graph_result(
                    {
                        "thread_id": "thread-mcp-tampered",
                        "status": "REPORT",
                        "pending_approval": False,
                        "verdict": "UNVERIFIED",
                        "state": {
                            "tool_events": [
                                {
                                    "type": "TOOL_CALL",
                                    "artifact": {
                                        "status": "READY",
                                        "kind": "mcp_tool_output",
                                        "relative_path": relative_path,
                                        "sha256": digest,
                                        "output_sha256": digest,
                                        "size_bytes": len(content),
                                    },
                                }
                            ]
                        },
                    }
                )

                self.assertNotIn(f"mcp_output_{digest[:16]}", {item.kind for item in store.artifacts("thread-mcp-tampered")})
                failure = next(item for item in store.events_after("thread-mcp-tampered", 0) if item.event_type == "MCP_ARTIFACT_REGISTER_FAILED")
                self.assertIn(
                    failure.payload["code"],
                    {"MCP_ARTIFACT_SIZE_MISMATCH", "MCP_ARTIFACT_INTEGRITY_MISMATCH"},
                )
            finally:
                store.close()

    def test_persists_patch_selection_audit_hashes_without_exposing_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = TaskStore(root / "state.sqlite")
            try:
                self._create_task(store, root, "thread-patch-selection")
                store.sync_graph_result(
                    {
                        "thread_id": "thread-patch-selection",
                        "status": "WAITING_APPROVAL",
                        "pending_approval": True,
                        "state": {
                            "tool_events": [
                                {
                                    "type": "PATCH_SELECTION_APPROVED",
                                    "selected_file_count": 1,
                                    "selection_sha256": "a" * 64,
                                    "selected_preview_sha256": "b" * 64,
                                    "selected_paths": ["src/main/java/OrderService.java"],
                                }
                            ]
                        },
                    }
                )
                event = next(item for item in store.recent_events("thread-patch-selection", limit=8) if item.event_type == "PATCH_SELECTION_APPROVED")
            finally:
                store.close()

        payload = event.to_public_dict()["payload"]
        self.assertEqual(1, payload["selected_file_count"])
        self.assertEqual("a" * 64, payload["selection_sha256"])
        self.assertEqual("b" * 64, payload["selected_preview_sha256"])
        self.assertNotIn("selected_paths", payload)

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
                self.assertNotIn("arguments", events[2].to_public_dict()["payload"])
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

    def test_archiving_terminal_task_moves_events_to_hashed_artifact_without_breaking_cursor_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database_path = root / "state.sqlite"
            store = TaskStore(database_path)
            try:
                self._create_task(store, root, "thread-event-archive")
                store.sync_graph_result(
                    {
                        "thread_id": "thread-event-archive",
                        "status": "REPORT",
                        "pending_approval": False,
                        "verdict": "UNVERIFIED",
                        "state": {
                            "error_summary": None,
                            "tool_events": [{"type": "FILE_READ", "duration_ms": 12, "arguments": {"path": "pom.xml"}}],
                        },
                    }
                )
                before = store.events_after("thread-event-archive", 0)
                archived = store.archive("thread-event-archive")
                after = store.events_after("thread-event-archive", 0)
                artifacts = {artifact.kind: artifact for artifact in store.artifacts("thread-event-archive")}

                self.assertIsNotNone(archived.archived_at)
                self.assertIn("event_archive", artifacts)
                self.assertEqual([event.sequence for event in after], list(range(1, len(after) + 1)))
                self.assertEqual([event.event_type for event in before], [event.event_type for event in after[: len(before)]])
                self.assertEqual("TASK_EVENTS_ARCHIVED", after[-2].event_type)
                self.assertEqual("TASK_ARCHIVED", after[-1].event_type)
                self.assertEqual((), store.events_after("thread-event-archive", after[-1].sequence))
            finally:
                store.close()

            reopened = TaskStore(database_path)
            try:
                replayed = reopened.events_after("thread-event-archive", 0)
                self.assertEqual("TASK_CREATED", replayed[0].event_type)
                self.assertEqual("TASK_ARCHIVED", replayed[-1].event_type)
                self.assertTrue(reopened.get("thread-event-archive").archived_at)
                archive_path = root / "runs" / "task-thread-event-archive" / "events.jsonl"
                archive_path.write_text("tampered\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "TASK_EVENT_ARCHIVE_INTEGRITY_MISMATCH"):
                    reopened.events_after("thread-event-archive", 0)
            finally:
                reopened.close()

    def test_retention_preview_and_explicit_batch_archive_keep_evidence_replayable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = TaskStore(root / "state.sqlite")
            try:
                self._create_task(store, root, "thread-retention-old")
                self._create_task(store, root, "thread-retention-running")
                store.sync_graph_result(
                    {
                        "thread_id": "thread-retention-old",
                        "status": "REPORT",
                        "pending_approval": False,
                        "verdict": "UNVERIFIED",
                        "state": {"tool_events": [{"type": "PLAN_GENERATED"}]},
                    }
                )
                # 用固定历史时间验证筛选，不依赖执行机器的系统时钟。
                store._connection.execute(
                    "UPDATE tasks SET updated_at = ? WHERE thread_id = ?",
                    ("2026-01-01T00:00:00+00:00", "thread-retention-old"),
                )
                store._connection.commit()

                before = store.events_after("thread-retention-old", 0)
                candidates = store.archive_candidates(
                    older_than_days=30,
                    limit=20,
                    now="2026-03-01T00:00:00+00:00",
                )

                self.assertEqual(["thread-retention-old"], [item.thread_id for item in candidates])
                self.assertGreater(candidates[0].live_event_count, 0)
                self.assertGreater(candidates[0].artifact_count, 0)
                self.assertIsNone(store.get("thread-retention-old").archived_at)
                self.assertIsNone(store.get("thread-retention-running").archived_at)

                result = store.archive_eligible(
                    older_than_days=30,
                    limit=20,
                    now="2026-03-01T00:00:00+00:00",
                )
                replayed = store.events_after("thread-retention-old", 0)

                self.assertEqual(["thread-retention-old"], [item.thread_id for item in result.archived])
                self.assertEqual((), result.blocked)
                self.assertFalse(result.to_dict()["deletion_performed"])
                self.assertIsNotNone(store.get("thread-retention-old").archived_at)
                self.assertIsNone(store.get("thread-retention-running").archived_at)
                self.assertEqual([event.event_type for event in before], [event.event_type for event in replayed[: len(before)]])
                self.assertEqual("TASK_ARCHIVED", replayed[-1].event_type)
                self.assertIn("report", {item.kind for item in store.artifacts("thread-retention-old")})
            finally:
                store.close()

    def test_retention_rejects_unbounded_requests_and_naive_clock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = TaskStore(root / "state.sqlite")
            try:
                with self.assertRaisesRegex(ValueError, "TASK_RETENTION_DAYS_INVALID"):
                    store.archive_candidates(older_than_days=-1)
                with self.assertRaisesRegex(ValueError, "TASK_RETENTION_LIMIT_INVALID"):
                    store.archive_candidates(older_than_days=1, limit=201)
                with self.assertRaisesRegex(ValueError, "TASK_RETENTION_TIME_INVALID"):
                    store.archive_candidates(older_than_days=1, now="2026-03-01T00:00:00")
            finally:
                store.close()

    def test_conversation_allows_only_one_active_task_and_preserves_task_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database = root / "state.sqlite"
            store = TaskStore(database)
            try:
                first = store.create(
                    thread_id="thread-conversation-1",
                    task_id="task-conversation-1",
                    project_id="project-1",
                    conversation_id="conversation-demo",
                    repository=root / "repo",
                    output_root=root / "runs",
                    task_mode="safe-isolated",
                    permission_mode="safe",
                    workspace_mode="worktree",
                )
                self.assertEqual("conversation-demo", first.conversation_id)
                with self.assertRaisesRegex(ValueError, "CONVERSATION_TASK_RUNNING"):
                    store.create(
                        thread_id="thread-conversation-2",
                        task_id="task-conversation-2",
                        project_id="project-1",
                        conversation_id="conversation-demo",
                        repository=root / "repo",
                        output_root=root / "runs",
                        task_mode="safe-isolated",
                        permission_mode="safe",
                        workspace_mode="worktree",
                    )

                store.sync_graph_result(
                    {
                        "thread_id": first.thread_id,
                        "status": "REPORT",
                        "pending_approval": False,
                        "verdict": "UNVERIFIED",
                        "state": {"tool_events": []},
                    },
                    execution_finished=True,
                )
                second = store.create(
                    thread_id="thread-conversation-2",
                    task_id="task-conversation-2",
                    project_id="project-1",
                    conversation_id="conversation-demo",
                    repository=root / "repo",
                    output_root=root / "runs",
                    task_mode="safe-isolated",
                    permission_mode="safe",
                    workspace_mode="worktree",
                )
                self.assertEqual(
                    [first.thread_id, second.thread_id],
                    [item.thread_id for item in store.list_for_conversation("conversation-demo")],
                )
            finally:
                store.close()

            reopened = TaskStore(database)
            try:
                with self.assertRaisesRegex(ValueError, "CONVERSATION_TASK_RUNNING"):
                    reopened.create(
                        thread_id="thread-conversation-3",
                        task_id="task-conversation-3",
                        project_id="project-1",
                        conversation_id="conversation-demo",
                        repository=root / "repo",
                        output_root=root / "runs",
                        task_mode="safe-isolated",
                        permission_mode="safe",
                        workspace_mode="worktree",
                    )
            finally:
                reopened.close()

    def test_recent_events_returns_bounded_tail_in_chronological_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = TaskStore(root / "state.sqlite")
            try:
                self._create_task(store, root, "thread-tail")
                store.sync_graph_result(
                    {
                        "thread_id": "thread-tail",
                        "status": "WAITING_APPROVAL",
                        "state": {"tool_events": [{"type": "FIRST"}, {"type": "SECOND"}]},
                    }
                )

                events = store.recent_events("thread-tail", limit=2)

                self.assertEqual(2, len(events))
                self.assertLess(events[0].sequence, events[1].sequence)
                self.assertEqual("SECOND", events[-1].event_type)
                with self.assertRaisesRegex(ValueError, "TASK_EVENT_LIMIT_INVALID"):
                    store.recent_events("thread-tail", limit=0)
            finally:
                store.close()

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

    def test_rename_updates_display_title_without_touching_task_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = TaskStore(root / "state.sqlite")
            try:
                self._create_task(store, root, "thread-rename")
                renamed = store.rename("thread-rename", "修复订单参数校验")
                self.assertEqual("修复订单参数校验", renamed.display_title)
                self.assertEqual("thread-rename", renamed.thread_id)
                self.assertEqual("task-thread-rename", renamed.task_id)
                self.assertIn(
                    "TASK_RENAMED",
                    [event.event_type for event in store.events_after("thread-rename", 0)],
                )
            finally:
                store.close()

    def test_runtime_failure_keeps_strong_status_and_recovers_missing_title(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = TaskStore(root / "state.sqlite")
            try:
                task = store.create(
                    thread_id="thread-legacy-failure",
                    task_id="task-legacy-failure",
                    project_id="project-1",
                    repository=root / "repo",
                    output_root=root / "runs",
                    task_mode="full-local",
                    permission_mode="full",
                    workspace_mode="local",
                )
                self.assertIsNone(task.display_title)
                store.mark_runtime_failure(task.thread_id, "TASK_RUNTIME_FAILED: GitCommandError")

                recovered = store.sync_graph_result(
                    {
                        "thread_id": task.thread_id,
                        "task_id": task.task_id,
                        "status": "PATCH",
                        "pending_approval": False,
                        "state": {
                            "task_operation": "research",
                            "task_description": "介绍项目 API_KEY=do-not-expose",
                            "tool_events": [],
                        },
                    },
                    execution_finished=False,
                )
                events = store.events_after(task.thread_id, 0)

                self.assertEqual("BLOCKED", recovered.status)
                self.assertEqual("BLOCKED", recovered.verdict)
                self.assertEqual("TASK_RUNTIME_FAILED: GitCommandError", recovered.error_summary)
                self.assertEqual("research", recovered.task_operation)
                self.assertEqual("介绍项目 API_KEY=[REDACTED]", recovered.display_title)
                self.assertEqual(1, sum(event.event_type == "TASK_METADATA_RECOVERED" for event in events))
                self.assertNotIn("do-not-expose", json.dumps([event.to_dict() for event in events], ensure_ascii=False))

                store.sync_graph_result(
                    {
                        "thread_id": task.thread_id,
                        "task_id": task.task_id,
                        "status": "REPORT",
                        "state": {
                            "task_operation": "research",
                            "task_description": "不得覆盖已恢复标题",
                            "tool_events": [],
                        },
                    }
                )
                self.assertEqual("介绍项目 API_KEY=[REDACTED]", store.get(task.thread_id).display_title)
                self.assertEqual(
                    1,
                    sum(
                        event.event_type == "TASK_METADATA_RECOVERED"
                        for event in store.events_after(task.thread_id, 0)
                    ),
                )
            finally:
                store.close()

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

    def test_outcome_summary_aggregates_terminal_status_and_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = TaskStore(root / "state.sqlite")
            try:
                entries = (
                    ("thread-pass-1", "REPORT", "PASSED"),
                    ("thread-pass-2", "REPORT", "PASSED"),
                    ("thread-block", "BLOCKED", "BLOCKED"),
                )
                for thread_id, status, verdict in entries:
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
                    store.sync_graph_result(
                        {
                            "thread_id": thread_id,
                            "status": status,
                            "pending_approval": False,
                            "verdict": verdict,
                            "state": {"tool_events": []},
                        }
                    )

                summary = store.outcome_summary()

                self.assertEqual(3, summary["total"])
                self.assertEqual(3, summary["terminal"])
                self.assertEqual(2, summary["passed"])
                self.assertEqual({"REPORT": 2, "BLOCKED": 1}, summary["by_status"])
                self.assertEqual({"PASSED": 2, "BLOCKED": 1}, summary["by_verdict"])
            finally:
                store.close()
