"""持久化 Agent 任务、状态事件与可恢复的 SSE 游标。"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4


_SENSITIVE_KEYS = frozenset({"api_key", "token", "password", "secret", "credential", "authorization"})
_INLINE_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|token|password|secret|authorization)\b\s*[:=]\s*([^\s,;]+)"
)
_TASK_TITLE_MAX_LENGTH = 80
_TASK_OPERATIONS = frozenset({"change", "research"})


@dataclass(frozen=True, slots=True)
class StoredTask:
    """不依赖 LangGraph checkpoint 的任务索引记录。"""

    thread_id: str
    trace_id: str
    task_id: str
    display_title: str | None
    project_id: str | None
    repository: str
    output_root: str
    task_mode: str
    task_operation: str
    permission_mode: str
    workspace_mode: str
    status: str
    pending_approval: bool
    verdict: str | None
    error_summary: str | None
    created_at: str
    updated_at: str
    heartbeat_at: str
    lease_expires_at: str | None
    cancellation_requested_at: str | None
    cancellation_reason: str | None
    archived_at: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "thread_id": self.thread_id,
            "trace_id": self.trace_id,
            "task_id": self.task_id,
            "display_title": self.display_title,
            "project_id": self.project_id,
            "repository": self.repository,
            "output_root": self.output_root,
            "task_mode": self.task_mode,
            "task_operation": self.task_operation,
            "permission_mode": self.permission_mode,
            "workspace_mode": self.workspace_mode,
            "status": self.status,
            "pending_approval": self.pending_approval,
            "verdict": self.verdict,
            "error_summary": self.error_summary,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "heartbeat_at": self.heartbeat_at,
            "lease_expires_at": self.lease_expires_at,
            "cancellation_requested_at": self.cancellation_requested_at,
            "cancellation_reason": self.cancellation_reason,
            "archived_at": self.archived_at,
            "interrupts": [],
        }


@dataclass(frozen=True, slots=True)
class StoredTaskEvent:
    """可用 sequence 断线续传的脱敏事件。"""

    sequence: int
    event_id: str
    trace_id: str
    event_type: str
    payload: dict[str, object]
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "event_id": self.event_id,
            "trace_id": self.trace_id,
            "type": self.event_type,
            "payload": self.payload,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class StoredTaskArtifact:
    """任务执行过程中生成的可审计产物元数据，不在 SQLite 内复制正文。"""

    thread_id: str
    kind: str
    relative_path: str
    sha256: str
    size_bytes: int
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "thread_id": self.thread_id,
            "kind": self.kind,
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class StoredTaskArtifactVersion:
    """某个任务产物的一份不可变历史快照。"""

    thread_id: str
    kind: str
    version: int
    relative_path: str
    sha256: str
    size_bytes: int
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "thread_id": self.thread_id,
            "kind": self.kind,
            "version": self.version,
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "created_at": self.created_at,
        }


class TaskStore:
    """任务 API 的 SQLite 索引，不替代 LangGraph 的 checkpoint。"""

    DEFAULT_LEASE_SECONDS = 15 * 60

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.expanduser().resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.database_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = RLock()
        self._initialize()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def create(
        self,
        *,
        thread_id: str,
        task_id: str,
        project_id: str | None,
        repository: Path,
        output_root: Path,
        task_mode: str,
        permission_mode: str,
        workspace_mode: str,
        task_operation: str = "change",
        trace_id: str | None = None,
        display_title: str | None = None,
    ) -> StoredTask:
        if not _is_safe_task_id(task_id):
            raise ValueError("INVALID_TASK_ID")
        if task_operation not in _TASK_OPERATIONS:
            raise ValueError("TASK_OPERATION_INVALID")
        now = self._now()
        resolved_trace_id = trace_id or self._new_trace_id()
        with self._lock:
            try:
                self._connection.execute(
                    """
                    INSERT INTO tasks(
                        thread_id, trace_id, task_id, display_title, project_id, repository, output_root, task_mode,
                        task_operation, permission_mode, workspace_mode, status, pending_approval, verdict,
                        error_summary, created_at, updated_at, heartbeat_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'RUNNING', 0, NULL, NULL, ?, ?, ?)
                    """,
                    (
                        thread_id,
                        resolved_trace_id,
                        task_id,
                        _normalize_task_title(display_title),
                        project_id,
                        str(repository),
                        str(output_root),
                        task_mode,
                        task_operation,
                        permission_mode,
                        workspace_mode,
                        now,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError("THREAD_ID_ALREADY_EXISTS") from error
            self._append_event_locked(
                thread_id,
                "TASK_CREATED",
                {"status": "RUNNING", "task_id": task_id, "task_operation": task_operation},
            )
            self._connection.commit()
            return self._get_locked(thread_id)

    def get(self, thread_id: str) -> StoredTask:
        with self._lock:
            return self._get_locked(thread_id)

    def list(self, limit: int = 100, *, include_archived: bool = False) -> tuple[StoredTask, ...]:
        with self._lock:
            query = "SELECT * FROM tasks"
            if not include_archived:
                query += " WHERE archived_at IS NULL"
            rows = self._connection.execute(f"{query} ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
            return tuple(self._task_from_row(row) for row in rows)

    def begin_execution(self, thread_id: str, *, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> StoredTask:
        """取得短租约后才允许后台执行，服务重启遗留任务可被超时回收。"""

        if lease_seconds <= 0:
            raise ValueError("INVALID_TASK_LEASE")
        with self._lock:
            task = self._get_locked(thread_id)
            if task.archived_at:
                raise ValueError("TASK_ARCHIVED")
            if task.cancellation_requested_at:
                raise ValueError("TASK_CANCELLATION_REQUESTED")
            if task.status in {"REPORT", "BLOCKED", "CANCELLED"}:
                raise ValueError("TASK_NOT_EXECUTABLE")
            now = self._now()
            self._connection.execute(
                """
                UPDATE tasks
                SET status = 'RUNNING', pending_approval = 0, updated_at = ?, heartbeat_at = ?, lease_expires_at = ?
                WHERE thread_id = ?
                """,
                (now, now, self._lease_deadline(lease_seconds), thread_id),
            )
            self._append_event_locked(thread_id, "TASK_EXECUTION_STARTED", {"status": "RUNNING"})
            self._connection.commit()
            return self._get_locked(thread_id)

    def renew_lease(self, thread_id: str, *, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> StoredTask:
        """后台执行器只续租自己的任务；终态、归档和已取消任务不能复活。"""

        with self._lock:
            task = self._get_locked(thread_id)
            if task.status not in {"RUNNING", "CANCELLATION_REQUESTED"} or not task.lease_expires_at:
                return task
            now = self._now()
            self._connection.execute(
                "UPDATE tasks SET heartbeat_at = ?, lease_expires_at = ?, updated_at = ? WHERE thread_id = ?",
                (now, self._lease_deadline(lease_seconds), now, thread_id),
            )
            self._connection.commit()
            return self._get_locked(thread_id)

    def request_cancellation(self, thread_id: str, reason: str | None = None) -> StoredTask:
        """记录取消请求；运行中的模型调用结束前绝不谎称其已经停止。"""

        with self._lock:
            task = self._get_locked(thread_id)
            if task.archived_at:
                raise ValueError("TASK_ARCHIVED")
            if task.status in {"REPORT", "BLOCKED", "CANCELLED"}:
                raise ValueError("TASK_NOT_CANCELLABLE")
            if task.cancellation_requested_at:
                return task
            now = self._now()
            message = reason.strip()[:500] if isinstance(reason, str) and reason.strip() else "用户请求取消任务。"
            if task.lease_expires_at:
                self._connection.execute(
                    """
                    UPDATE tasks
                    SET status = 'CANCELLATION_REQUESTED', pending_approval = 0, cancellation_requested_at = ?,
                        cancellation_reason = ?, error_summary = ?, updated_at = ?
                    WHERE thread_id = ?
                    """,
                    (now, message, message, now, thread_id),
                )
                event_type = "TASK_CANCELLATION_REQUESTED"
                payload = {"status": "CANCELLATION_REQUESTED", "reason": message}
            else:
                self._connection.execute(
                    """
                    UPDATE tasks
                    SET status = 'CANCELLED', pending_approval = 0, verdict = 'CANCELLED', cancellation_requested_at = ?,
                        cancellation_reason = ?, error_summary = ?, updated_at = ?, heartbeat_at = ?
                    WHERE thread_id = ?
                    """,
                    (now, message, message, now, now, thread_id),
                )
                event_type = "TASK_CANCELLED"
                payload = {"status": "CANCELLED", "verdict": "CANCELLED", "reason": message}
            self._append_event_locked(thread_id, event_type, payload)
            self._connection.commit()
            return self._get_locked(thread_id)

    def complete_cancellation(self, thread_id: str) -> StoredTask:
        """仅在后台调用已退出后写入已取消终态。"""

        with self._lock:
            task = self._get_locked(thread_id)
            if not task.cancellation_requested_at:
                raise ValueError("TASK_CANCELLATION_NOT_REQUESTED")
            if task.status == "CANCELLED":
                return task
            now = self._now()
            self._connection.execute(
                """
                UPDATE tasks
                SET status = 'CANCELLED', pending_approval = 0, verdict = 'CANCELLED', lease_expires_at = NULL,
                    error_summary = ?, updated_at = ?, heartbeat_at = ?
                WHERE thread_id = ?
                """,
                (task.cancellation_reason or "用户请求取消任务。", now, now, thread_id),
            )
            self._append_event_locked(
                thread_id,
                "TASK_CANCELLED",
                {"status": "CANCELLED", "verdict": "CANCELLED", "reason": task.cancellation_reason},
            )
            self._connection.commit()
            return self._get_locked(thread_id)

    def reap_expired_leases(self, *, now: str | None = None) -> tuple[StoredTask, ...]:
        """将失去心跳的后台任务标记为阻断，避免重启后永久显示运行中。"""

        reference = now or self._now()
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT thread_id FROM tasks
                WHERE lease_expires_at IS NOT NULL AND lease_expires_at <= ?
                  AND status IN ('RUNNING', 'CANCELLATION_REQUESTED')
                """,
                (reference,),
            ).fetchall()
            recovered: list[StoredTask] = []
            for row in rows:
                thread_id = row["thread_id"]
                self._connection.execute(
                    """
                    UPDATE tasks
                    SET status = 'BLOCKED', pending_approval = 0, verdict = 'BLOCKED', lease_expires_at = NULL,
                        error_summary = 'TASK_LEASE_EXPIRED', updated_at = ?, heartbeat_at = ?
                    WHERE thread_id = ?
                    """,
                    (reference, reference, thread_id),
                )
                self._append_event_locked(
                    thread_id,
                    "TASK_LEASE_EXPIRED",
                    {"status": "BLOCKED", "verdict": "BLOCKED", "code": "TASK_LEASE_EXPIRED"},
                )
                recovered.append(self._get_locked(thread_id))
            self._connection.commit()
            return tuple(recovered)

    def archive(self, thread_id: str) -> StoredTask:
        """归档只隐藏任务列表，不删除证据、产物或 LangGraph checkpoint。"""

        with self._lock:
            task = self._get_locked(thread_id)
            if task.archived_at:
                return task
            if task.status not in {"REPORT", "BLOCKED", "CANCELLED"} or task.lease_expires_at:
                raise ValueError("TASK_NOT_ARCHIVABLE")
            now = self._now()
            self._connection.execute(
                "UPDATE tasks SET archived_at = ?, updated_at = ? WHERE thread_id = ?",
                (now, now, thread_id),
            )
            self._append_event_locked(thread_id, "TASK_ARCHIVED", {"archived_at": now})
            self._connection.commit()
            return self._get_locked(thread_id)

    def sync_graph_result(self, result: dict[str, object], *, execution_finished: bool = True) -> StoredTask:
        """将 checkpoint 快照投影为任务索引和只含摘要的事件流。"""

        thread_id = str(result["thread_id"])
        state = result.get("state")
        graph_state = state if isinstance(state, dict) else {}
        status = str(result.get("status") or graph_state.get("status") or "RUNNING")
        pending_approval = bool(result.get("pending_approval", graph_state.get("pending_approval", False)))
        verdict = result.get("verdict")
        error_summary = graph_state.get("error_summary")
        if not isinstance(error_summary, str):
            error_summary = None
        tool_events = graph_state.get("tool_events")
        events = tool_events if isinstance(tool_events, list) else []
        graph_task_operation = graph_state.get("task_operation")
        if graph_task_operation is not None and graph_task_operation not in _TASK_OPERATIONS:
            raise ValueError("TASK_OPERATION_INVALID")

        with self._lock:
            try:
                previous = self._get_locked(thread_id)
            except ValueError:
                # API 服务升级或重启前已有的 LangGraph checkpoint 也必须可被重新发现。
                permission_mode = str(graph_state.get("permission_mode") or "safe")
                workspace_mode = str(graph_state.get("workspace_mode") or "worktree")
                task_mode = "safe-isolated" if permission_mode == "safe" and workspace_mode == "worktree" else "full-local"
                previous = self.create(
                    thread_id=thread_id,
                    task_id=str(result["task_id"]),
                    project_id=graph_state.get("project_id") if isinstance(graph_state.get("project_id"), str) else None,
                    repository=Path(str(graph_state.get("repository") or "")),
                    output_root=Path(str(graph_state.get("output_root") or "")),
                    task_mode=task_mode,
                    task_operation=str(graph_task_operation or "change"),
                    permission_mode=permission_mode,
                    workspace_mode=workspace_mode,
                )
            now = self._now()
            task_operation = str(graph_task_operation or previous.task_operation)
            if previous.cancellation_requested_at or previous.error_summary in {"TASK_LEASE_EXPIRED"} or (
                previous.error_summary is not None and previous.error_summary.startswith("TASK_RUNTIME_FAILED:")
            ):
                # 取消、租约回收和运行时故障是任务服务的强事实，旧 checkpoint 不得覆盖为等待审批或成功。
                if task_operation != previous.task_operation:
                    self._connection.execute(
                        "UPDATE tasks SET task_operation = ? WHERE thread_id = ?",
                        (task_operation, thread_id),
                    )
                for index, event in enumerate(events):
                    if isinstance(event, dict):
                        self._append_event_locked(thread_id, str(event.get("type", "EVIDENCE")), event, source_index=index)
                self._sync_artifacts_locked(previous, graph_state)
                self._connection.commit()
                return self._get_locked(thread_id)
            self._connection.execute(
                """
                UPDATE tasks
                SET task_operation = ?, status = ?, pending_approval = ?, verdict = ?, error_summary = ?,
                    updated_at = ?, heartbeat_at = ?, lease_expires_at = ?
                WHERE thread_id = ?
                """,
                (task_operation, status, int(pending_approval), verdict, error_summary, now, now, None if execution_finished else previous.lease_expires_at, thread_id),
            )
            if (
                previous.status != status
                or previous.pending_approval != pending_approval
                or previous.verdict != verdict
                or previous.error_summary != error_summary
            ):
                self._append_event_locked(
                    thread_id,
                    "TASK_STATE",
                    {
                        "status": status,
                        "pending_approval": pending_approval,
                        "verdict": verdict,
                        "error_summary": error_summary,
                    },
                )
            for index, event in enumerate(events):
                if isinstance(event, dict):
                    self._append_event_locked(thread_id, str(event.get("type", "EVIDENCE")), event, source_index=index)
            # 报告必须使用刚投影的 verdict/status，不能引用同步前的快照。
            self._sync_artifacts_locked(self._get_locked(thread_id), graph_state)
            self._connection.commit()
            return self._get_locked(thread_id)

    def mark_runtime_failure(self, thread_id: str, error_code: str) -> StoredTask:
        with self._lock:
            now = self._now()
            self._connection.execute(
                """
                UPDATE tasks
                SET status = 'BLOCKED', pending_approval = 0, verdict = 'BLOCKED', lease_expires_at = NULL,
                    error_summary = ?, updated_at = ?, heartbeat_at = ?
                WHERE thread_id = ?
                """,
                (error_code, now, now, thread_id),
            )
            self._append_event_locked(
                thread_id,
                "TASK_RUNTIME_FAILED",
                {"status": "BLOCKED", "verdict": "BLOCKED", "code": error_code},
            )
            self._connection.commit()
            return self._get_locked(thread_id)

    def events_after(self, thread_id: str, sequence: int, *, limit: int | None = None) -> tuple[StoredTaskEvent, ...]:
        if sequence < 0:
            raise ValueError("TASK_EVENT_CURSOR_INVALID")
        if limit is not None and not 1 <= limit <= 1000:
            raise ValueError("TASK_EVENT_LIMIT_INVALID")
        with self._lock:
            self._get_locked(thread_id)
            query = """
                SELECT event.sequence, event.event_id, event.event_type, event.payload_json, event.created_at, task.trace_id
                FROM task_events AS event
                INNER JOIN tasks AS task ON task.thread_id = event.thread_id
                WHERE event.thread_id = ? AND event.sequence > ?
                ORDER BY event.sequence ASC
            """
            parameters: tuple[object, ...] = (thread_id, sequence)
            if limit is not None:
                query += " LIMIT ?"
                parameters = (*parameters, limit)
            rows = self._connection.execute(query, parameters).fetchall()
        return tuple(
            StoredTaskEvent(
                sequence=int(row["sequence"]),
                event_id=row["event_id"],
                trace_id=row["trace_id"],
                event_type=row["event_type"],
                payload=json.loads(row["payload_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        )

    def telemetry(self, thread_id: str) -> dict[str, object]:
        """按已持久化证据事件重建遥测，进程重启后仍可查询。"""

        task = self.get(thread_id)
        return _telemetry_summary(task, [event.payload for event in self.events_after(thread_id, 0)])

    def artifacts(self, thread_id: str) -> tuple[StoredTaskArtifact, ...]:
        """返回当前任务的产物清单，正文仍保留在任务产物目录中。"""

        with self._lock:
            self._get_locked(thread_id)
            rows = self._connection.execute(
                """
                SELECT thread_id, kind, relative_path, sha256, size_bytes, created_at, updated_at
                FROM task_artifacts
                WHERE thread_id = ?
                ORDER BY kind ASC
                """,
                (thread_id,),
            ).fetchall()
        return tuple(self._artifact_from_row(row) for row in rows)

    def read_artifact(self, thread_id: str, kind: str, *, max_bytes: int = 512 * 1024) -> tuple[StoredTaskArtifact, str]:
        """按受控 kind 读取文本产物，避免 API 将任意本机路径暴露给前端。"""

        with self._lock:
            row = self._connection.execute(
                """
                SELECT thread_id, kind, relative_path, sha256, size_bytes, created_at, updated_at
                FROM task_artifacts
                WHERE thread_id = ? AND kind = ?
                """,
                (thread_id, kind),
            ).fetchone()
            if not row:
                raise ValueError("TASK_ARTIFACT_NOT_FOUND")
            artifact = self._artifact_from_row(row)
            return artifact, self._read_artifact_content_locked(self._get_locked(thread_id), artifact, max_bytes=max_bytes)

    def artifact_versions(self, thread_id: str, kind: str) -> tuple[StoredTaskArtifactVersion, ...]:
        """按新到旧返回受控产物的历史版本元数据，不暴露任意任务目录文件。"""

        with self._lock:
            self._get_locked(thread_id)
            rows = self._connection.execute(
                """
                SELECT thread_id, kind, version, relative_path, sha256, size_bytes, created_at
                FROM task_artifact_versions
                WHERE thread_id = ? AND kind = ?
                ORDER BY version DESC
                """,
                (thread_id, kind),
            ).fetchall()
        return tuple(self._artifact_version_from_row(row) for row in rows)

    def read_artifact_version(
        self,
        thread_id: str,
        kind: str,
        version: int,
        *,
        max_bytes: int = 512 * 1024,
    ) -> tuple[StoredTaskArtifactVersion, str]:
        """读取指定不可变版本，并在返回前重新核对内容哈希。"""

        if version < 1:
            raise ValueError("TASK_ARTIFACT_VERSION_NOT_FOUND")
        with self._lock:
            row = self._connection.execute(
                """
                SELECT thread_id, kind, version, relative_path, sha256, size_bytes, created_at
                FROM task_artifact_versions
                WHERE thread_id = ? AND kind = ? AND version = ?
                """,
                (thread_id, kind, version),
            ).fetchone()
            if not row:
                raise ValueError("TASK_ARTIFACT_VERSION_NOT_FOUND")
            artifact = self._artifact_version_from_row(row)
            return artifact, self._read_artifact_content_locked(self._get_locked(thread_id), artifact, max_bytes=max_bytes)

    def _get_locked(self, thread_id: str) -> StoredTask:
        row = self._connection.execute("SELECT * FROM tasks WHERE thread_id = ?", (thread_id,)).fetchone()
        if not row:
            raise ValueError("TASK_NOT_FOUND")
        return self._task_from_row(row)

    def _append_event_locked(
        self,
        thread_id: str,
        event_type: str,
        payload: dict[str, object],
        *,
        source_index: int | None = None,
    ) -> None:
        if source_index is not None:
            existing = self._connection.execute(
                "SELECT 1 FROM task_events WHERE thread_id = ? AND source_index = ?",
                (thread_id, source_index),
            ).fetchone()
            if existing:
                return
        sequence = int(
            self._connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM task_events WHERE thread_id = ?", (thread_id,)
            ).fetchone()[0]
        ) + 1
        event_id = f"{thread_id}:{sequence}"
        self._connection.execute(
            """
            INSERT INTO task_events(thread_id, sequence, event_id, event_type, payload_json, source_index, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                thread_id,
                sequence,
                event_id,
                event_type,
                json.dumps(_redact(payload), ensure_ascii=False, sort_keys=True, default=str),
                source_index,
                self._now(),
            ),
        )

    def _sync_artifacts_locked(self, task: StoredTask, graph_state: dict[str, object]) -> None:
        """原子写当前产物，并将内容变化追加为不可变历史版本。"""

        artifacts: dict[str, tuple[str, bytes]] = {}
        plan = graph_state.get("plan")
        if isinstance(plan, dict):
            artifacts["plan_json"] = ("plan.json", _json_bytes(plan))
            artifacts["plan_markdown"] = ("plan.md", _plan_markdown(plan).encode("utf-8"))

        patch_proposal = graph_state.get("patch_proposal")
        if isinstance(patch_proposal, dict):
            artifacts["patch_proposal"] = ("patch-proposal.json", _json_bytes(patch_proposal))

        git_diff = graph_state.get("git_diff")
        if isinstance(git_diff, str) and git_diff:
            artifacts["git_diff"] = ("changes.diff", git_diff.encode("utf-8"))

        verification = graph_state.get("verification_result")
        if isinstance(verification, dict):
            artifacts["verification"] = ("verification.json", _json_bytes(verification))

        telemetry = _telemetry_summary(task, graph_state.get("tool_events"))
        artifacts["telemetry"] = ("telemetry.json", _json_bytes(telemetry))

        report = _report_markdown(task, graph_state)
        artifacts["report"] = ("report.md", report.encode("utf-8"))

        for kind, (file_name, content) in artifacts.items():
            try:
                path = self._artifact_path_locked(task, file_name)
                now = self._now()
                content_sha256 = sha256(content).hexdigest()
                current = self._connection.execute(
                    """
                    SELECT thread_id, kind, relative_path, sha256, size_bytes, created_at, updated_at
                    FROM task_artifacts WHERE thread_id = ? AND kind = ?
                    """,
                    (task.thread_id, kind),
                ).fetchone()
                if current and current["sha256"] == content_sha256:
                    # 同一 checkpoint 重放不能制造伪版本，也不会改写历史文件。
                    continue
                version = int(
                    self._connection.execute(
                        """
                        SELECT COALESCE(MAX(version), 0) FROM task_artifact_versions
                        WHERE thread_id = ? AND kind = ?
                        """,
                        (task.thread_id, kind),
                    ).fetchone()[0]
                ) + 1
                history_relative_path = _artifact_history_relative_path(kind, version, file_name, content_sha256)
                history_path = self._artifact_path_locked(task, history_relative_path)
                # 先持久化不可变快照，再替换便于 UI 读取的当前版本。
                _atomic_write(history_path, content)
                _atomic_write(path, content)
                self._connection.execute(
                    """
                    INSERT INTO task_artifacts(
                        thread_id, kind, relative_path, sha256, size_bytes, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(thread_id, kind) DO UPDATE SET
                        relative_path = excluded.relative_path,
                        sha256 = excluded.sha256,
                        size_bytes = excluded.size_bytes,
                        updated_at = excluded.updated_at
                    """,
                    (
                        task.thread_id,
                        kind,
                        file_name,
                        content_sha256,
                        len(content),
                        now,
                        now,
                    ),
                )
                self._connection.execute(
                    """
                    INSERT INTO task_artifact_versions(
                        thread_id, kind, version, relative_path, sha256, size_bytes, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (task.thread_id, kind, version, history_relative_path, content_sha256, len(content), now),
                )
            except OSError as error:
                self._append_event_locked(
                    task.thread_id,
                    "TASK_ARTIFACT_WRITE_FAILED",
                    {"kind": kind, "code": type(error).__name__},
                )

    @staticmethod
    def _artifact_path_locked(task: StoredTask, relative_path: str) -> Path:
        """任务产物只能落在 output_root/task_id 内，杜绝 checkpoint 注入路径。"""

        root = Path(task.output_root).expanduser().resolve()
        directory = (root / task.task_id).resolve()
        path = (directory / relative_path).resolve()
        try:
            path.relative_to(directory)
        except ValueError as error:
            raise OSError("TASK_ARTIFACT_PATH_ESCAPE")
        if path == directory:
            raise OSError("TASK_ARTIFACT_PATH_ESCAPE")
        return path

    def _read_artifact_content_locked(
        self,
        task: StoredTask,
        artifact: StoredTaskArtifact | StoredTaskArtifactVersion,
        *,
        max_bytes: int,
    ) -> str:
        if artifact.size_bytes > max_bytes:
            raise ValueError("TASK_ARTIFACT_TOO_LARGE")
        path = self._artifact_path_locked(task, artifact.relative_path)
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as error:
            raise ValueError("TASK_ARTIFACT_UNAVAILABLE") from error
        if sha256(content.encode("utf-8")).hexdigest() != artifact.sha256:
            raise ValueError("TASK_ARTIFACT_INTEGRITY_MISMATCH")
        return content

    def _initialize(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    thread_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL,
                    task_id TEXT NOT NULL UNIQUE,
                    display_title TEXT,
                    project_id TEXT,
                    repository TEXT NOT NULL,
                    output_root TEXT NOT NULL,
                    task_mode TEXT NOT NULL,
                    task_operation TEXT NOT NULL DEFAULT 'change' CHECK(task_operation IN ('change', 'research')),
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
                CREATE TABLE IF NOT EXISTS task_events (
                    thread_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    source_index INTEGER,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(thread_id, sequence),
                    UNIQUE(thread_id, source_index),
                    FOREIGN KEY(thread_id) REFERENCES tasks(thread_id)
                );
                CREATE INDEX IF NOT EXISTS idx_task_events_thread_sequence
                    ON task_events(thread_id, sequence);
                CREATE TABLE IF NOT EXISTS task_artifacts (
                    thread_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(thread_id, kind),
                    FOREIGN KEY(thread_id) REFERENCES tasks(thread_id)
                );
                CREATE INDEX IF NOT EXISTS idx_task_artifacts_thread
                    ON task_artifacts(thread_id, kind);
                CREATE TABLE IF NOT EXISTS task_artifact_versions (
                    thread_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    relative_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(thread_id, kind, version),
                    FOREIGN KEY(thread_id) REFERENCES tasks(thread_id)
                );
                CREATE INDEX IF NOT EXISTS idx_task_artifact_versions_thread_kind
                    ON task_artifact_versions(thread_id, kind, version DESC);
                """
            )
            self._ensure_column_locked("tasks", "lease_expires_at", "TEXT")
            self._ensure_column_locked("tasks", "cancellation_requested_at", "TEXT")
            self._ensure_column_locked("tasks", "cancellation_reason", "TEXT")
            self._ensure_column_locked("tasks", "archived_at", "TEXT")
            self._ensure_column_locked("tasks", "trace_id", "TEXT")
            self._ensure_column_locked("tasks", "display_title", "TEXT")
            self._ensure_column_locked("tasks", "task_operation", "TEXT NOT NULL DEFAULT 'change'")
            self._backfill_trace_ids_locked()
            self._connection.commit()

    def _ensure_column_locked(self, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in self._connection.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            self._connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _backfill_trace_ids_locked(self) -> None:
        """旧数据库没有 trace_id 时只回填一次，并保留其余任务审计记录。"""

        rows = self._connection.execute(
            "SELECT thread_id FROM tasks WHERE trace_id IS NULL OR trim(trace_id) = ''"
        ).fetchall()
        for row in rows:
            self._connection.execute(
                "UPDATE tasks SET trace_id = ? WHERE thread_id = ?",
                (self._new_trace_id(), row["thread_id"]),
            )

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> StoredTask:
        if row["task_operation"] not in _TASK_OPERATIONS:
            raise ValueError("TASK_OPERATION_INVALID")
        return StoredTask(
            thread_id=row["thread_id"],
            trace_id=row["trace_id"],
            task_id=row["task_id"],
            display_title=row["display_title"],
            project_id=row["project_id"],
            repository=row["repository"],
            output_root=row["output_root"],
            task_mode=row["task_mode"],
            task_operation=row["task_operation"],
            permission_mode=row["permission_mode"],
            workspace_mode=row["workspace_mode"],
            status=row["status"],
            pending_approval=bool(row["pending_approval"]),
            verdict=row["verdict"],
            error_summary=row["error_summary"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            heartbeat_at=row["heartbeat_at"],
            lease_expires_at=row["lease_expires_at"],
            cancellation_requested_at=row["cancellation_requested_at"],
            cancellation_reason=row["cancellation_reason"],
            archived_at=row["archived_at"],
        )

    @staticmethod
    def _artifact_from_row(row: sqlite3.Row) -> StoredTaskArtifact:
        return StoredTaskArtifact(
            thread_id=row["thread_id"],
            kind=row["kind"],
            relative_path=row["relative_path"],
            sha256=row["sha256"],
            size_bytes=int(row["size_bytes"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _artifact_version_from_row(row: sqlite3.Row) -> StoredTaskArtifactVersion:
        return StoredTaskArtifactVersion(
            thread_id=row["thread_id"],
            kind=row["kind"],
            version=int(row["version"]),
            relative_path=row["relative_path"],
            sha256=row["sha256"],
            size_bytes=int(row["size_bytes"]),
            created_at=row["created_at"],
        )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _lease_deadline(lease_seconds: int) -> str:
        return (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()

    @staticmethod
    def _new_trace_id() -> str:
        return f"trace-{uuid4().hex}"


def _normalize_task_title(value: str | None) -> str | None:
    """生成侧栏展示标题，不复制完整任务正文或常见凭据值。"""

    if not value:
        return None
    normalized = " ".join(value.split())
    if not normalized:
        return None
    redacted = _INLINE_SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", normalized)
    if len(redacted) <= _TASK_TITLE_MAX_LENGTH:
        return redacted
    return f"{redacted[: _TASK_TITLE_MAX_LENGTH - 3].rstrip()}..."


def _redact(value: Any, key: str | None = None) -> Any:
    if key and key.lower() in _SENSITIVE_KEYS:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(name): _redact(item, str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _atomic_write(path: Path, content: bytes) -> None:
    """先完整写临时文件再替换，任务中断时不会留下半份报告。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def _artifact_history_relative_path(kind: str, version: int, file_name: str, content_sha256: str) -> str:
    """历史路径完全由服务端生成，避免 kind 或文件名把路径带出任务目录。"""

    safe_kind = "".join(character if character.isalnum() or character in {"_", "-"} else "_" for character in kind)
    safe_file_name = Path(file_name).name
    return f"history/{safe_kind}/v{version:04d}-{content_sha256[:12]}-{safe_file_name}"


def _json_bytes(value: dict[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str).encode("utf-8") + b"\n"


def _plan_markdown(plan: dict[str, object]) -> str:
    lines = ["# 修改计划", ""]
    summary = plan.get("summary") or plan.get("problem_summary")
    if isinstance(summary, str) and summary:
        lines.extend(["## 问题摘要", "", summary, ""])
    for key, title in (("candidate_files", "候选文件"), ("steps", "修改步骤"), ("verification", "验证建议"), ("risks", "风险与假设")):
        value = plan.get(key)
        if not value:
            continue
        lines.extend([f"## {title}", ""])
        if isinstance(value, list):
            lines.extend(f"- {item}" for item in value)
        else:
            lines.append(str(value))
        lines.append("")
    lines.extend(["## 结构化原文", "", "详见同目录 `plan.json`。", ""])
    return "\n".join(lines)


def _report_markdown(task: StoredTask, graph_state: dict[str, object]) -> str:
    """报告只陈述已记录的状态和证据，不把计划误写成修复成功。"""

    status = str(graph_state.get("status") or task.status)
    verdict = graph_state.get("verdict") or task.verdict or "UNVERIFIED"
    error_summary = graph_state.get("error_summary") or task.error_summary
    telemetry = _telemetry_summary(task, graph_state.get("tool_events"))
    model = telemetry["model"]
    budget = telemetry["budget"]
    lines = [
        "# RepoPilot 任务报告",
        "",
        f"- 任务 ID：`{task.task_id}`",
        f"- 线程 ID：`{task.thread_id}`",
        f"- Trace ID：`{task.trace_id}`",
        f"- 产品模式：`{task.task_mode}`",
        f"- 当前状态：`{status}`",
        f"- 验证结论：`{verdict}`",
        "",
        "## 产物说明",
        "",
        "- `plan.json` / `plan.md`：经研究生成的修改计划（存在时）。",
        "- `patch-proposal.json`：待执行补丁提案（存在时）。",
        "- `changes.diff`：实际 Git Diff（存在时）。",
        "- `verification.json`：Maven 或其他验证结果（存在时）。",
        "- `telemetry.json`：节点耗时与模型供应商回传的用量汇总（存在时）。",
    ]
    lines.extend(
        [
            "",
            "## 运行遥测",
            "",
            f"- 已完成节点：`{telemetry['node_count']}` 个，总耗时：`{telemetry['node_total_duration_ms']}` ms。",
            f"- 模型用量已回传操作：`{model['reported_operations']}` 个；未回传操作：`{model['unavailable_operations']}` 个。",
        ]
    )
    if model["reported_operations"]:
        lines.append(
            f"- Token：输入 `{'{:,}'.format(model['input_tokens'])}`，输出 `{'{:,}'.format(model['output_tokens'])}`，合计 `{'{:,}'.format(model['total_tokens'])}`。"
        )
    if model["estimated_cost"] is not None and model["currency"]:
        lines.append(f"- 估算成本：`{model['currency']} {model['estimated_cost']:.8f}`。")
    if budget["configured"]:
        limits = []
        if budget["max_total_tokens"] is not None:
            limits.append(f"Token 上限 `{'{:,}'.format(budget['max_total_tokens'])}`")
        if budget["max_estimated_cost"] is not None:
            limits.append(f"成本上限 `{budget['currency']} {budget['max_estimated_cost']:.8f}`")
        lines.append(f"- 任务预算：{'，'.join(limits)}；状态：`{budget['status']}`。")
    if error_summary:
        lines.extend(["", "## 阻断或错误摘要", "", str(error_summary)])
    lines.extend(["", "## 结论", ""])
    if verdict == "PASSED":
        lines.append("已记录真实 Diff 与成功验证证据。")
    elif verdict == "UNVERIFIED":
        lines.append("当前结果尚未形成完整验证证据，不能视为修复成功。")
    else:
        lines.append("任务未完成或已被阻断，请结合上述产物与事件时间线排查。")
    lines.append("")
    return "\n".join(lines)


def _telemetry_summary(task: StoredTask, raw_events: object) -> dict[str, object]:
    """从图事件生成可展示的最小遥测快照，不保留模型原文或工具完整输出。"""

    events = raw_events if isinstance(raw_events, list) else []
    nodes: list[dict[str, object]] = []
    input_tokens = output_tokens = total_tokens = 0
    reported_operations = unavailable_operations = 0
    costs: list[float] = []
    currencies: set[str] = set()
    budget: dict[str, object] = {
        "configured": False,
        "max_total_tokens": None,
        "max_estimated_cost": None,
        "currency": None,
        "status": "NOT_CONFIGURED",
        "code": None,
    }
    for item in events:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "NODE_COMPLETED":
            duration = item.get("duration_ms")
            if isinstance(duration, int) and duration >= 0 and isinstance(item.get("node"), str):
                nodes.append({"node": item["node"], "duration_ms": duration})
        if item.get("type") == "TASK_BUDGET_SNAPSHOT":
            budget = {
                "configured": item.get("configured") is True,
                "max_total_tokens": item.get("max_total_tokens") if isinstance(item.get("max_total_tokens"), int) else None,
                "max_estimated_cost": item.get("max_estimated_cost") if isinstance(item.get("max_estimated_cost"), (int, float)) else None,
                "currency": item.get("currency") if isinstance(item.get("currency"), str) else None,
                "status": "ACTIVE" if item.get("configured") is True else "NOT_CONFIGURED",
                "code": None,
            }
        if item.get("type") == "GRAPH_BLOCKED" and isinstance(item.get("code"), str) and item["code"].startswith("MODEL_"):
            budget["status"] = "BLOCKED"
            budget["code"] = item["code"]
        if item.get("type") != "MODEL_USAGE":
            continue
        if item.get("reported") is not True:
            unavailable_operations += 1
            continue
        reported_operations += 1
        input_tokens += _non_negative_integer(item.get("input_tokens"))
        output_tokens += _non_negative_integer(item.get("output_tokens"))
        total_tokens += _non_negative_integer(item.get("total_tokens"))
        cost = item.get("estimated_cost")
        currency = item.get("currency")
        if isinstance(cost, (int, float)) and cost >= 0 and isinstance(currency, str) and currency:
            costs.append(float(cost))
            currencies.add(currency)
        else:
            currencies.add("")
    estimated_cost = round(sum(costs), 8) if reported_operations and len(costs) == reported_operations and len(currencies) == 1 else None
    currency = next(iter(currencies)) if estimated_cost is not None else None
    return {
        "trace_id": task.trace_id,
        "task_id": task.task_id,
        "thread_id": task.thread_id,
        "nodes": nodes,
        "node_count": len(nodes),
        "node_total_duration_ms": sum(item["duration_ms"] for item in nodes),
        "model": {
            "reported_operations": reported_operations,
            "unavailable_operations": unavailable_operations,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "estimated_cost": estimated_cost,
            "currency": currency,
        },
        "budget": budget,
    }


def _non_negative_integer(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def _is_safe_task_id(task_id: str) -> bool:
    return bool(task_id) and task_id not in {".", ".."} and "/" not in task_id and "\\" not in task_id
