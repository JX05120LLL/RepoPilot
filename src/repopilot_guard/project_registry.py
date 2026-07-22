"""本地项目注册表：保存已授权的项目目录与任务工作区关联。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    """一个可被后续任务复用的本地项目。"""

    project_id: str
    display_name: str
    root_path: Path
    is_git_repository: bool
    created_at: str
    last_used_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "display_name": self.display_name,
            "root_path": str(self.root_path),
            "is_git_repository": self.is_git_repository,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
        }


class ProjectRegistry:
    """使用状态 SQLite 保存项目，不扫描或索引未被用户添加的目录。"""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.expanduser().resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        # FastAPI 会在线程池中处理同步端点，注册表连接必须允许同一进程跨线程读取。
        self._connection = sqlite3.connect(self.database_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._initialize()

    def close(self) -> None:
        self._connection.close()

    def add(self, root_path: Path, display_name: str | None = None) -> ProjectRecord:
        root = root_path.expanduser().resolve()
        if not root.is_dir():
            raise ValueError("PROJECT_DIRECTORY_NOT_FOUND")
        existing = self._connection.execute(
            "SELECT * FROM projects WHERE root_path = ?", (str(root),)
        ).fetchone()
        if existing:
            self._touch(existing["project_id"])
            return self.get(existing["project_id"])

        now = self._now()
        project_id = f"project-{uuid4().hex[:12]}"
        self._connection.execute(
            """
            INSERT INTO projects(project_id, display_name, root_path, is_git_repository, created_at, last_used_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (project_id, display_name or root.name, str(root), int(self._is_git_repository(root)), now, now),
        )
        self._connection.commit()
        return self.get(project_id)

    def get(self, project_id: str) -> ProjectRecord:
        row = self._connection.execute("SELECT * FROM projects WHERE project_id = ?", (project_id,)).fetchone()
        if not row:
            raise ValueError("PROJECT_NOT_FOUND")
        return self._refresh_git_state(self._record(row))

    def list(self) -> tuple[ProjectRecord, ...]:
        rows = self._connection.execute("SELECT * FROM projects ORDER BY last_used_at DESC, display_name ASC").fetchall()
        return tuple(self._refresh_git_state(self._record(row)) for row in rows)

    def remove(self, project_id: str) -> bool:
        cursor = self._connection.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
        self._connection.commit()
        return cursor.rowcount == 1

    def touch(self, project_id: str) -> ProjectRecord:
        self._touch(project_id)
        return self.get(project_id)

    def record_workspace(
        self,
        *,
        task_id: str,
        project_id: str | None,
        mode: str,
        workspace_path: Path,
        base_commit: str,
        created_at: datetime,
    ) -> None:
        self._connection.execute(
            """
            INSERT OR REPLACE INTO task_workspaces(task_id, project_id, mode, workspace_path, base_commit, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (task_id, project_id, mode, str(workspace_path), base_commit, created_at.isoformat()),
        )
        self._connection.commit()
        if project_id:
            self._touch(project_id)

    def get_workspace(self, task_id: str, project_id: str) -> Path:
        """只返回属于该项目的已登记任务工作区，避免任意目录被批量索引。"""

        row = self._connection.execute(
            "SELECT workspace_path FROM task_workspaces WHERE task_id = ? AND project_id = ?",
            (task_id, project_id),
        ).fetchone()
        if not row:
            raise ValueError("TASK_WORKSPACE_NOT_FOUND")
        workspace_path = Path(row["workspace_path"])
        if not workspace_path.is_dir():
            raise ValueError("TASK_WORKSPACE_UNAVAILABLE")
        return workspace_path

    def _initialize(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                project_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                root_path TEXT NOT NULL UNIQUE,
                is_git_repository INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                last_used_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS task_workspaces (
                task_id TEXT PRIMARY KEY,
                project_id TEXT,
                mode TEXT NOT NULL,
                workspace_path TEXT NOT NULL,
                base_commit TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(project_id)
            );
            """
        )
        self._connection.commit()

    def _touch(self, project_id: str) -> None:
        self._connection.execute("UPDATE projects SET last_used_at = ? WHERE project_id = ?", (self._now(), project_id))
        self._connection.commit()

    @staticmethod
    def _is_git_repository(root: Path) -> bool:
        return (root / ".git").exists()

    def _refresh_git_state(self, record: ProjectRecord) -> ProjectRecord:
        """同步用户在注册后执行 git init 或移除 .git 的轻量状态变化。"""

        current_state = self._is_git_repository(record.root_path)
        if current_state == record.is_git_repository:
            return record
        self._connection.execute(
            "UPDATE projects SET is_git_repository = ? WHERE project_id = ?",
            (int(current_state), record.project_id),
        )
        self._connection.commit()
        return ProjectRecord(
            project_id=record.project_id,
            display_name=record.display_name,
            root_path=record.root_path,
            is_git_repository=current_state,
            created_at=record.created_at,
            last_used_at=record.last_used_at,
        )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _record(row: sqlite3.Row) -> ProjectRecord:
        return ProjectRecord(
            project_id=row["project_id"],
            display_name=row["display_name"],
            root_path=Path(row["root_path"]),
            is_git_repository=bool(row["is_git_repository"]),
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
        )
