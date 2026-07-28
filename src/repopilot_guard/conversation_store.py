"""本地会话草稿：允许先组织问题，再决定是否关联代码项目。"""

from __future__ import annotations

import sqlite3
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from uuid import uuid4


_MODES = frozenset({"goal", "plan"})
_INLINE_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|token|password|secret|authorization)\b\s*[:=]\s*([^\s,;]+)"
)


@dataclass(frozen=True, slots=True)
class ConversationRecord:
    """一个尚未或不需要进入 Agent 执行流的本地会话。"""

    conversation_id: str
    project_id: str | None
    display_title: str
    mode: str
    created_at: str
    updated_at: str
    archived_at: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "conversation_id": self.conversation_id,
            "project_id": self.project_id,
            "display_title": self.display_title,
            "mode": self.mode,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "archived_at": self.archived_at,
        }


class ConversationStore:
    """持久化侧栏会话元数据，不保存模型上下文或代码文件内容。"""

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

    def create(self, *, project_id: str | None, display_title: str | None, mode: str) -> ConversationRecord:
        resolved_mode = self._normalize_mode(mode)
        now = self._now()
        conversation_id = f"conversation-{uuid4().hex[:12]}"
        title = self._normalize_title(display_title) or "未命名对话"
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO conversations(
                    conversation_id, project_id, display_title, mode, created_at, updated_at, archived_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL)
                """,
                (conversation_id, project_id, title, resolved_mode, now, now),
            )
            self._connection.commit()
            return self._get_locked(conversation_id)

    def list(self, *, include_archived: bool = False) -> tuple[ConversationRecord, ...]:
        with self._lock:
            query = "SELECT * FROM conversations"
            if not include_archived:
                query += " WHERE archived_at IS NULL"
            query += " ORDER BY updated_at DESC, display_title ASC"
            return tuple(self._record(row) for row in self._connection.execute(query).fetchall())

    def get(self, conversation_id: str) -> ConversationRecord:
        with self._lock:
            return self._get_locked(conversation_id)

    def update(
        self,
        conversation_id: str,
        *,
        display_title: str | None = None,
        project_id: str | None | object = ...,
        mode: str | None = None,
    ) -> ConversationRecord:
        """仅允许更新展示元数据；`project_id=None` 表示明确移出项目。"""

        fields: list[str] = []
        values: list[object] = []
        if display_title is not None:
            fields.append("display_title = ?")
            values.append(self._normalize_title(display_title, required=True))
        if project_id is not ...:
            fields.append("project_id = ?")
            values.append(project_id)
        if mode is not None:
            fields.append("mode = ?")
            values.append(self._normalize_mode(mode))
        if not fields:
            raise ValueError("CONVERSATION_UPDATE_EMPTY")
        with self._lock:
            self._get_locked(conversation_id)
            fields.append("updated_at = ?")
            values.extend((self._now(), conversation_id))
            self._connection.execute(
                f"UPDATE conversations SET {', '.join(fields)} WHERE conversation_id = ?",
                tuple(values),
            )
            self._connection.commit()
            return self._get_locked(conversation_id)

    def archive(self, conversation_id: str) -> ConversationRecord:
        with self._lock:
            record = self._get_locked(conversation_id)
            if record.archived_at:
                return record
            now = self._now()
            self._connection.execute(
                "UPDATE conversations SET archived_at = ?, updated_at = ? WHERE conversation_id = ?",
                (now, now, conversation_id),
            )
            self._connection.commit()
            return self._get_locked(conversation_id)

    def restore(self, conversation_id: str) -> ConversationRecord:
        with self._lock:
            record = self._get_locked(conversation_id)
            if not record.archived_at:
                return record
            self._connection.execute(
                "UPDATE conversations SET archived_at = NULL, updated_at = ? WHERE conversation_id = ?",
                (self._now(), conversation_id),
            )
            self._connection.commit()
            return self._get_locked(conversation_id)

    def _initialize(self) -> None:
        with self._lock:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id TEXT PRIMARY KEY,
                    project_id TEXT,
                    display_title TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK(mode IN ('goal', 'plan')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    archived_at TEXT
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversations_project_updated ON conversations(project_id, updated_at DESC)"
            )
            self._connection.commit()

    def _get_locked(self, conversation_id: str) -> ConversationRecord:
        row = self._connection.execute(
            "SELECT * FROM conversations WHERE conversation_id = ?", (conversation_id,)
        ).fetchone()
        if not row:
            raise ValueError("CONVERSATION_NOT_FOUND")
        return self._record(row)

    @staticmethod
    def _record(row: sqlite3.Row) -> ConversationRecord:
        return ConversationRecord(
            conversation_id=row["conversation_id"],
            project_id=row["project_id"],
            display_title=row["display_title"],
            mode=row["mode"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            archived_at=row["archived_at"],
        )

    @staticmethod
    def _normalize_title(value: str | None, *, required: bool = False) -> str | None:
        if value is None:
            return None
        title = " ".join(value.split())
        if not title and not required:
            return None
        redacted = _INLINE_SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", title)
        if not 1 <= len(redacted) <= 80:
            raise ValueError("CONVERSATION_TITLE_INVALID")
        return redacted

    @staticmethod
    def _normalize_mode(value: str) -> str:
        if value not in _MODES:
            raise ValueError("CONVERSATION_MODE_INVALID")
        return value

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
