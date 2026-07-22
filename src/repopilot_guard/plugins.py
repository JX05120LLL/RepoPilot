"""本地插件包的登记、完整性校验与审计。

插件不是新的权限通道。它只能携带已声明的 Skill、MCP 配置和未来 UI 元数据，
并且必须先经过本地用户显式安装和启用，运行时才可能引用其中的内容。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PLUGIN_MANIFEST_NAME = "repopilot-plugin.json"
MAX_PLUGIN_FILES = 512
MAX_PLUGIN_FILE_BYTES = 1024 * 1024
_PLUGIN_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_VERSION_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$")


class PluginError(ValueError):
    """对外只提供稳定错误码，不泄漏插件正文或配置内容。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class PluginManifest:
    """插件清单；所有路径均为相对插件根目录的受控路径。"""

    plugin_id: str
    name: str
    version: str
    description: str
    skills_root: str | None = None
    mcp_config: str | None = None
    ui: dict[str, str] | None = None

    @classmethod
    def load(cls, root: Path) -> "PluginManifest":
        manifest_path = root / PLUGIN_MANIFEST_NAME
        try:
            raw = manifest_path.read_bytes()
        except OSError as error:
            raise PluginError("PLUGIN_MANIFEST_NOT_FOUND", "插件目录缺少 repopilot-plugin.json 清单。") from error
        if len(raw) > 64 * 1024:
            raise PluginError("PLUGIN_MANIFEST_TOO_LARGE", "插件清单超过 64 KiB 安全上限。")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PluginError("PLUGIN_MANIFEST_INVALID", "插件清单必须是 UTF-8 JSON 对象。") from error
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise PluginError("PLUGIN_MANIFEST_INVALID", "插件清单 schema_version 必须为 1。")

        plugin_id = _required_text(payload, "id", 64)
        name = _required_text(payload, "name", 128)
        version = _required_text(payload, "version", 64)
        description = _required_text(payload, "description", 1024)
        if not _PLUGIN_ID_PATTERN.fullmatch(plugin_id):
            raise PluginError("PLUGIN_ID_INVALID", "插件 ID 只能使用小写字母、数字和连字符。")
        if not _VERSION_PATTERN.fullmatch(version):
            raise PluginError("PLUGIN_VERSION_INVALID", "插件版本格式无效。")

        skills_root = _optional_relative_path(payload.get("skills_root"), root, "skills_root")
        mcp_config = _optional_relative_path(payload.get("mcp_config"), root, "mcp_config")
        ui = _optional_ui(payload.get("ui"))
        return cls(plugin_id, name, version, description, skills_root, mcp_config, ui)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "id": self.plugin_id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "skills_root": self.skills_root,
            "mcp_config": self.mcp_config,
            "ui": dict(self.ui) if self.ui else None,
        }


@dataclass(frozen=True, slots=True)
class PluginRecord:
    """SQLite 中登记的插件及当前完整性结论。"""

    plugin_id: str
    root_path: Path
    manifest: PluginManifest
    package_sha256: str
    enabled: bool
    integrity_status: str
    installed_at: str
    updated_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "plugin_id": self.plugin_id,
            "root_path": str(self.root_path),
            "manifest": self.manifest.to_dict(),
            "package_sha256": self.package_sha256,
            "enabled": self.enabled,
            "integrity_status": self.integrity_status,
            "installed_at": self.installed_at,
            "updated_at": self.updated_at,
            "active": self.enabled and self.integrity_status == "VERIFIED",
        }


class PluginRegistry:
    """使用状态 SQLite 管理插件；移除登记绝不删除用户的插件目录。"""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.expanduser().resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.database_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._initialize()

    def close(self) -> None:
        self._connection.close()

    def install(self, source: Path) -> PluginRecord:
        root = _plugin_root(source)
        manifest = PluginManifest.load(root)
        package_sha256 = _package_sha256(root)
        existing = self._connection.execute("SELECT * FROM plugins WHERE plugin_id = ?", (manifest.plugin_id,)).fetchone()
        if existing is not None and Path(existing["root_path"]) != root:
            raise PluginError("PLUGIN_ID_ALREADY_INSTALLED", "同一插件 ID 已登记到其他目录；请先显式移除旧登记。")
        now = self._now()
        action = "PLUGIN_INSTALLED" if existing is None else "PLUGIN_REINSTALLED"
        installed_at = now if existing is None else existing["installed_at"]
        enabled = 1 if existing is None else existing["enabled"]
        self._connection.execute(
            """
            INSERT INTO plugins(plugin_id, root_path, manifest_json, package_sha256, enabled, installed_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(plugin_id) DO UPDATE SET root_path=excluded.root_path, manifest_json=excluded.manifest_json,
                package_sha256=excluded.package_sha256, enabled=excluded.enabled, updated_at=excluded.updated_at
            """,
            (manifest.plugin_id, str(root), _canonical_json(manifest.to_dict()), package_sha256, enabled, installed_at, now),
        )
        self._audit(manifest.plugin_id, action, "READY", {"package_sha256": package_sha256, "enabled": bool(enabled)})
        self._connection.commit()
        return self.get(manifest.plugin_id)

    def get(self, plugin_id: str) -> PluginRecord:
        row = self._connection.execute("SELECT * FROM plugins WHERE plugin_id = ?", (plugin_id,)).fetchone()
        if row is None:
            raise PluginError("PLUGIN_NOT_FOUND", "未找到已登记插件。")
        return self._record(row)

    def list(self) -> tuple[PluginRecord, ...]:
        rows = self._connection.execute("SELECT * FROM plugins ORDER BY plugin_id ASC").fetchall()
        return tuple(self._record(row) for row in rows)

    def enable(self, plugin_id: str) -> PluginRecord:
        record = self.get(plugin_id)
        if record.integrity_status != "VERIFIED":
            self._audit(plugin_id, "PLUGIN_ENABLE_BLOCKED", "BLOCKED", {"integrity_status": record.integrity_status})
            self._connection.commit()
            raise PluginError("PLUGIN_INTEGRITY_CHECK_FAILED", "插件内容已变化或不可读取；请审查后重新安装。")
        self._set_enabled(plugin_id, True, "PLUGIN_ENABLED")
        return self.get(plugin_id)

    def disable(self, plugin_id: str) -> PluginRecord:
        self.get(plugin_id)
        self._set_enabled(plugin_id, False, "PLUGIN_DISABLED")
        return self.get(plugin_id)

    def remove(self, plugin_id: str) -> bool:
        self.get(plugin_id)
        self._audit(plugin_id, "PLUGIN_REMOVED", "READY", {"directory_deleted": False})
        cursor = self._connection.execute("DELETE FROM plugins WHERE plugin_id = ?", (plugin_id,))
        self._connection.commit()
        return cursor.rowcount == 1

    def audit(self, plugin_id: str | None = None, limit: int = 100) -> tuple[dict[str, object], ...]:
        if not 1 <= limit <= 500:
            raise PluginError("PLUGIN_AUDIT_LIMIT_INVALID", "审计查询条数必须在 1 到 500 之间。")
        if plugin_id:
            rows = self._connection.execute(
                "SELECT * FROM plugin_audit WHERE plugin_id = ? ORDER BY id DESC LIMIT ?", (plugin_id, limit)
            ).fetchall()
        else:
            rows = self._connection.execute("SELECT * FROM plugin_audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return tuple(
            {
                "plugin_id": row["plugin_id"],
                "action": row["action"],
                "status": row["status"],
                "details": json.loads(row["details_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        )

    def active_skill_roots(self) -> tuple[Path, ...]:
        """只返回已启用且完整性通过的 Skill 目录，调用方仍须进行 Skill 自身校验。"""

        roots: list[Path] = []
        for record in self.list():
            if not record.enabled or record.integrity_status != "VERIFIED" or not record.manifest.skills_root:
                continue
            candidate = (record.root_path / record.manifest.skills_root).resolve()
            if candidate.is_dir() and _is_within(candidate, record.root_path):
                roots.append(candidate)
        return tuple(roots)

    def active_mcp_configs(self) -> tuple[Path, ...]:
        """返回经过完整性校验的配置路径；调用方仍必须走 MCP 权限与 Schema 校验。"""

        configs: list[Path] = []
        for record in self.list():
            if not record.enabled or record.integrity_status != "VERIFIED" or not record.manifest.mcp_config:
                continue
            candidate = (record.root_path / record.manifest.mcp_config).resolve()
            if candidate.is_file() and _is_within(candidate, record.root_path):
                configs.append(candidate)
        return tuple(configs)

    def _set_enabled(self, plugin_id: str, enabled: bool, action: str) -> None:
        now = self._now()
        self._connection.execute("UPDATE plugins SET enabled = ?, updated_at = ? WHERE plugin_id = ?", (int(enabled), now, plugin_id))
        self._audit(plugin_id, action, "READY", {"enabled": enabled})
        self._connection.commit()

    def _record(self, row: sqlite3.Row) -> PluginRecord:
        root = Path(row["root_path"])
        try:
            manifest = PluginManifest.load(root)
            current_hash = _package_sha256(root)
            integrity_status = "VERIFIED" if current_hash == row["package_sha256"] else "TAMPERED"
        except PluginError:
            manifest = _stored_manifest(row["manifest_json"])
            integrity_status = "UNAVAILABLE"
        return PluginRecord(
            plugin_id=row["plugin_id"],
            root_path=root,
            manifest=manifest,
            package_sha256=row["package_sha256"],
            enabled=bool(row["enabled"]),
            integrity_status=integrity_status,
            installed_at=row["installed_at"],
            updated_at=row["updated_at"],
        )

    def _audit(self, plugin_id: str, action: str, status: str, details: dict[str, object]) -> None:
        self._connection.execute(
            "INSERT INTO plugin_audit(plugin_id, action, status, details_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (plugin_id, action, status, _canonical_json(details), self._now()),
        )

    def _initialize(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS plugins (
                plugin_id TEXT PRIMARY KEY,
                root_path TEXT NOT NULL,
                manifest_json TEXT NOT NULL,
                package_sha256 TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                installed_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS plugin_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plugin_id TEXT NOT NULL,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                details_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_plugin_audit_plugin_created ON plugin_audit(plugin_id, created_at DESC);
            """
        )
        self._connection.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()


def _plugin_root(source: Path) -> Path:
    root = source.expanduser().resolve()
    if not root.is_dir():
        raise PluginError("PLUGIN_DIRECTORY_NOT_FOUND", "插件路径必须是存在的目录。")
    if root.is_symlink():
        raise PluginError("PLUGIN_SYMLINK_BLOCKED", "插件根目录不能是符号链接。")
    return root


def _required_text(payload: dict[str, Any], key: str, limit: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise PluginError("PLUGIN_MANIFEST_INVALID", f"插件清单缺少有效的 {key} 字段。")
    return value.strip()


def _optional_relative_path(value: object, root: Path, key: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PluginError("PLUGIN_MANIFEST_INVALID", f"插件清单的 {key} 必须是非空相对路径。")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise PluginError("PLUGIN_PATH_ESCAPE", f"插件清单的 {key} 不能离开插件根目录。")
    candidate = (root / relative).resolve()
    if not _is_within(candidate, root):
        raise PluginError("PLUGIN_PATH_ESCAPE", f"插件清单的 {key} 不能离开插件根目录。")
    return relative.as_posix()


def _optional_ui(value: object) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or any(not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()):
        raise PluginError("PLUGIN_MANIFEST_INVALID", "插件 ui 元数据必须是字符串键值对象。")
    if len(value) > 16:
        raise PluginError("PLUGIN_MANIFEST_INVALID", "插件 ui 元数据条目过多。")
    return {key: item for key, item in value.items()}


def _package_sha256(root: Path) -> str:
    entries: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if ".git" in path.parts or "__pycache__" in path.parts:
            continue
        if path.is_symlink():
            raise PluginError("PLUGIN_SYMLINK_BLOCKED", "插件包不能包含符号链接。")
        if not path.is_file():
            continue
        if len(entries) >= MAX_PLUGIN_FILES:
            raise PluginError("PLUGIN_FILE_LIMIT_REACHED", "插件文件数量超过安全上限。")
        if path.stat().st_size > MAX_PLUGIN_FILE_BYTES:
            raise PluginError("PLUGIN_FILE_TOO_LARGE", "插件包含超过 1 MiB 的文件。")
        resolved = path.resolve()
        if not _is_within(resolved, root):
            raise PluginError("PLUGIN_PATH_ESCAPE", "插件文件不能通过链接离开插件根目录。")
        entries.append((resolved.relative_to(root).as_posix(), hashlib.sha256(resolved.read_bytes()).hexdigest()))
    if not any(name == PLUGIN_MANIFEST_NAME for name, _ in entries):
        raise PluginError("PLUGIN_MANIFEST_NOT_FOUND", "插件目录缺少 repopilot-plugin.json 清单。")
    return _canonical_hash(entries)


def _stored_manifest(encoded: str) -> PluginManifest:
    try:
        payload = json.loads(encoded)
        if not isinstance(payload, dict):
            raise ValueError
        return PluginManifest(
            plugin_id=str(payload["id"]),
            name=str(payload["name"]),
            version=str(payload["version"]),
            description=str(payload["description"]),
            skills_root=payload.get("skills_root"),
            mcp_config=payload.get("mcp_config"),
            ui=payload.get("ui"),
        )
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise PluginError("PLUGIN_STORED_MANIFEST_INVALID", "已登记插件元数据不可读取。") from error


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
