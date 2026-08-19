"""本地项目注册表：保存已授权的项目目录与任务工作区关联。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from repopilot_guard.capability_profiles import CapabilityProfile, CapabilityProfileScanner, normalize_confirmations


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    """一个可被后续任务复用的本地项目。"""

    project_id: str
    display_name: str
    root_path: Path
    is_git_repository: bool
    created_at: str
    last_used_at: str
    archived_at: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "display_name": self.display_name,
            "root_path": str(self.root_path),
            "is_git_repository": self.is_git_repository,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "archived_at": self.archived_at,
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
            if existing["archived_at"]:
                self.restore(existing["project_id"])
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
        record = self.get(project_id)
        # 用户明确添加目录后才执行一次受限静态扫描；不索引、更不执行项目命令。
        self.capability_profile(record.project_id)
        return record

    def get(self, project_id: str) -> ProjectRecord:
        row = self._connection.execute("SELECT * FROM projects WHERE project_id = ?", (project_id,)).fetchone()
        if not row:
            raise ValueError("PROJECT_NOT_FOUND")
        return self._refresh_git_state(self._record(row))

    def list(self, *, include_archived: bool = False) -> tuple[ProjectRecord, ...]:
        query = "SELECT * FROM projects"
        if not include_archived:
            query += " WHERE archived_at IS NULL"
        query += " ORDER BY last_used_at DESC, display_name ASC"
        rows = self._connection.execute(query).fetchall()
        return tuple(self._refresh_git_state(self._record(row)) for row in rows)

    def remove(self, project_id: str) -> bool:
        cursor = self._connection.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
        self._connection.commit()
        return cursor.rowcount == 1

    def rename(self, project_id: str, display_name: str) -> ProjectRecord:
        """仅修改本地展示名称，不影响项目路径、Git 或关联任务。"""

        name = self._normalize_display_name(display_name)
        if not self._connection.execute(
            "SELECT 1 FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone():
            raise ValueError("PROJECT_NOT_FOUND")
        self._connection.execute(
            "UPDATE projects SET display_name = ?, last_used_at = ? WHERE project_id = ?",
            (name, self._now(), project_id),
        )
        self._connection.commit()
        return self.get(project_id)

    def archive(self, project_id: str) -> ProjectRecord:
        """归档只从活动项目树隐藏项目，绝不删除目录、任务或向量数据。"""

        record = self.get(project_id)
        if record.archived_at:
            return record
        now = self._now()
        self._connection.execute(
            "UPDATE projects SET archived_at = ?, last_used_at = ? WHERE project_id = ?",
            (now, now, project_id),
        )
        self._connection.commit()
        return self.get(project_id)

    def restore(self, project_id: str) -> ProjectRecord:
        record = self.get(project_id)
        if not record.archived_at:
            return record
        self._connection.execute(
            "UPDATE projects SET archived_at = NULL, last_used_at = ? WHERE project_id = ?",
            (self._now(), project_id),
        )
        self._connection.commit()
        return self.get(project_id)

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

    def capability_profile(self, project_id: str) -> CapabilityProfile:
        """刷新扫描事实；源码变化使旧确认失效，防止把过期约束注入新任务。"""

        project = self.get(project_id)
        scanned = CapabilityProfileScanner().scan(project_id, project.root_path)
        row = self._connection.execute(
            "SELECT profile_sha256, confirmed_at, business_rules_json, protected_paths_json FROM project_capability_profiles WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if row and row["profile_sha256"] == scanned.profile_sha256:
            return CapabilityProfile(
                project_id, scanned.facts, scanned.profile_sha256, row["confirmed_at"],
                tuple(json.loads(row["business_rules_json"])), tuple(json.loads(row["protected_paths_json"])),
            )
        now = self._now()
        self._connection.execute(
            """
            INSERT INTO project_capability_profiles(project_id, profile_sha256, facts_json, confirmed_at, business_rules_json, protected_paths_json, updated_at)
            VALUES (?, ?, ?, NULL, '[]', '[]', ?)
            ON CONFLICT(project_id) DO UPDATE SET profile_sha256 = excluded.profile_sha256, facts_json = excluded.facts_json,
                confirmed_at = NULL, business_rules_json = '[]', protected_paths_json = '[]', updated_at = excluded.updated_at
            """,
            (project_id, scanned.profile_sha256, json.dumps(scanned.facts, ensure_ascii=False, sort_keys=True), now),
        )
        self._connection.commit()
        return scanned

    def confirm_capability_profile(
        self, project_id: str, profile_sha256: str, business_rules: tuple[str, ...], protected_paths: tuple[str, ...]
    ) -> CapabilityProfile:
        current = self.capability_profile(project_id)
        if profile_sha256 != current.profile_sha256:
            raise ValueError("CAPABILITY_PROFILE_STALE")
        rules = normalize_confirmations(business_rules, code="CAPABILITY_PROFILE_RULES_INVALID")
        paths = normalize_confirmations(protected_paths, code="CAPABILITY_PROFILE_PATHS_INVALID")
        now = self._now()
        self._connection.execute(
            "UPDATE project_capability_profiles SET confirmed_at = ?, business_rules_json = ?, protected_paths_json = ?, updated_at = ? WHERE project_id = ?",
            (now, json.dumps(rules, ensure_ascii=False), json.dumps(paths, ensure_ascii=False), now, project_id),
        )
        self._connection.commit()
        return CapabilityProfile(project_id, current.facts, current.profile_sha256, now, rules, paths)

    def _initialize(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                project_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                root_path TEXT NOT NULL UNIQUE,
                is_git_repository INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                last_used_at TEXT NOT NULL,
                archived_at TEXT
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
            CREATE TABLE IF NOT EXISTS project_capability_profiles (
                project_id TEXT PRIMARY KEY,
                profile_sha256 TEXT NOT NULL,
                facts_json TEXT NOT NULL,
                confirmed_at TEXT,
                business_rules_json TEXT NOT NULL DEFAULT '[]',
                protected_paths_json TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(project_id)
            );
            """
        )
        self._ensure_column("projects", "archived_at", "TEXT")
        self._connection.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {
            row["name"]
            for row in self._connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            self._connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

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
            archived_at=record.archived_at,
        )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _normalize_display_name(value: str) -> str:
        name = " ".join(value.split())
        if not 1 <= len(name) <= 80:
            raise ValueError("PROJECT_DISPLAY_NAME_INVALID")
        return name

    @staticmethod
    def _record(row: sqlite3.Row) -> ProjectRecord:
        return ProjectRecord(
            project_id=row["project_id"],
            display_name=row["display_name"],
            root_path=Path(row["root_path"]),
            is_git_repository=bool(row["is_git_repository"]),
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
            archived_at=row["archived_at"],
        )
