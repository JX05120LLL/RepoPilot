"""本地插件包的登记、完整性校验与审计。

插件不是新的权限通道。它只能携带已声明的 Skill、MCP 配置和未来 UI 元数据，
并且必须先经过本地用户显式安装和启用，运行时才可能引用其中的内容。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import sqlite3
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from repopilot_guard import __version__
from repopilot_guard.processes import hidden_process_kwargs


PLUGIN_MANIFEST_NAME = "repopilot-plugin.json"
PLUGIN_SIGNATURE_NAME = "repopilot-plugin.sig"
MAX_PLUGIN_FILES = 512
MAX_PLUGIN_FILE_BYTES = 1024 * 1024
_PLUGIN_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_TRUST_KEY_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
_GIT_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_GIT_SCP_URL_PATTERN = re.compile(r"^git@[A-Za-z0-9.-]+:[A-Za-z0-9._/-]+$")
_HOOK_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
_HOOK_CONTEXT_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_HOOK_EVENTS = frozenset({"task_intake", "plan_approval", "execution_approval"})
_HOOK_DECISIONS = frozenset({"allow", "ask", "deny"})
_VERSION_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$")
_SEMVER_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_VERSION_CONSTRAINT_PATTERN = re.compile(r"^(>=|>|<=|<|==)?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
REPOPILOT_VERSION = __version__


class PluginError(ValueError):
    """对外只提供稳定错误码，不泄漏插件正文或配置内容。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class PluginTrustKey:
    """本机显式信任的 Ed25519 发布者公钥，不保存任何私钥。"""

    key_id: str
    public_key_base64: str
    fingerprint: str
    created_at: str

    def to_dict(self) -> dict[str, str]:
        return {
            "key_id": self.key_id,
            "fingerprint": self.fingerprint,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class PluginSignature:
    """Detached 签名文件，签名内容包含发布者、算法和完整包哈希。"""

    key_id: str
    package_sha256: str
    signature_base64: str
    source_lock: PluginSourceLock | None = None

    @classmethod
    def load(cls, root: Path) -> "PluginSignature":
        path = root / PLUGIN_SIGNATURE_NAME
        try:
            raw = path.read_bytes()
        except FileNotFoundError as error:
            raise PluginError("PLUGIN_SIGNATURE_NOT_FOUND", "插件缺少 repopilot-plugin.sig 签名文件。") from error
        except OSError as error:
            raise PluginError("PLUGIN_SIGNATURE_UNREADABLE", "插件签名文件不可读取。") from error
        if len(raw) > 16 * 1024:
            raise PluginError("PLUGIN_SIGNATURE_INVALID", "插件签名文件超过 16 KiB 安全上限。")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PluginError("PLUGIN_SIGNATURE_INVALID", "插件签名文件必须是 UTF-8 JSON 对象。") from error
        if not isinstance(payload, dict) or payload.get("schema_version") != 1 or payload.get("algorithm") != "ed25519":
            raise PluginError("PLUGIN_SIGNATURE_INVALID", "插件签名必须使用 schema_version=1 和 ed25519。")
        key_id = payload.get("key_id")
        package_sha256 = payload.get("package_sha256")
        signature_base64 = payload.get("signature")
        source_lock = _optional_source_lock(payload.get("source_lock"))
        if not isinstance(key_id, str) or not _TRUST_KEY_ID_PATTERN.fullmatch(key_id):
            raise PluginError("PLUGIN_SIGNATURE_INVALID", "插件签名的 key_id 格式无效。")
        if not isinstance(package_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", package_sha256):
            raise PluginError("PLUGIN_SIGNATURE_INVALID", "插件签名的 package_sha256 格式无效。")
        if not isinstance(signature_base64, str) or len(signature_base64) > 256:
            raise PluginError("PLUGIN_SIGNATURE_INVALID", "插件签名内容格式无效。")
        try:
            signature = base64.b64decode(signature_base64, validate=True)
        except (binascii.Error, ValueError) as error:
            raise PluginError("PLUGIN_SIGNATURE_INVALID", "插件签名不是合法 Base64。") from error
        if len(signature) != 64:
            raise PluginError("PLUGIN_SIGNATURE_INVALID", "Ed25519 插件签名长度无效。")
        return cls(key_id=key_id, package_sha256=package_sha256, signature_base64=signature_base64, source_lock=source_lock)

    def signed_payload(self) -> bytes:
        payload: dict[str, object] = {
            "schema_version": 1,
            "algorithm": "ed25519",
            "key_id": self.key_id,
            "package_sha256": self.package_sha256,
        }
        if self.source_lock is not None:
            payload["source_lock"] = self.source_lock.to_dict()
        return _canonical_json(payload).encode("utf-8")


@dataclass(frozen=True, slots=True)
class PluginSourceLock:
    """签名保护的 Git 来源锚点；安装时只复验本地 checkout，不联网拉取。"""

    repository: str
    revision: str

    def to_dict(self) -> dict[str, str]:
        return {"repository": self.repository, "revision": self.revision}


@dataclass(frozen=True, slots=True)
class PluginHook:
    """插件签名包中的声明式 Hook，不携带脚本、命令或网络配置。"""

    plugin_id: str
    hook_id: str
    event: str
    decision: str
    message: str
    context: dict[str, str]

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.hook_id,
            "event": self.event,
            "decision": self.decision,
            "message": self.message,
            "context": dict(self.context),
        }


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
    requires_repopilot: str | None = None
    hooks: tuple[PluginHook, ...] = ()

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
        requires_repopilot = _optional_version_constraint(payload.get("requires_repopilot"))
        hooks = _optional_hooks(payload.get("hooks"), plugin_id)
        return cls(
            plugin_id=plugin_id,
            name=name,
            version=version,
            description=description,
            skills_root=skills_root,
            mcp_config=mcp_config,
            ui=ui,
            requires_repopilot=requires_repopilot,
            hooks=hooks,
        )

    def is_compatible_with(self, repopilot_version: str | None = None) -> bool:
        """只接受确定性 SemVer 约束，避免插件清单引入复杂脚本或解释器。"""

        if self.requires_repopilot is None:
            return True
        current = _parse_semver(repopilot_version or REPOPILOT_VERSION)
        return all(_matches_version_constraint(current, item) for item in self.requires_repopilot.split(","))

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
            "requires_repopilot": self.requires_repopilot,
            "hooks": [hook.to_dict() for hook in self.hooks],
        }


@dataclass(frozen=True, slots=True)
class PluginRecord:
    """SQLite 中登记的插件及当前完整性结论。"""

    plugin_id: str
    source_path: Path
    root_path: Path
    manifest: PluginManifest
    package_sha256: str
    enabled: bool
    integrity_status: str
    compatibility_status: str
    signature_status: str
    signing_key_id: str | None
    source_lock_status: str
    installed_at: str
    updated_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "plugin_id": self.plugin_id,
            "manifest": self.manifest.to_dict(),
            "package_sha256": self.package_sha256,
            "enabled": self.enabled,
            "integrity_status": self.integrity_status,
            "compatibility_status": self.compatibility_status,
            "signature_status": self.signature_status,
            "signing_key_id": self.signing_key_id,
            "source_lock_status": self.source_lock_status,
            "installed_at": self.installed_at,
            "updated_at": self.updated_at,
            "active": (
                self.enabled
                and self.integrity_status == "VERIFIED"
                and self.compatibility_status == "COMPATIBLE"
                and self.signature_status == "VERIFIED"
            ),
        }


@dataclass(frozen=True, slots=True)
class PluginVersion:
    """已落入受控快照目录的可回退插件版本。"""

    plugin_id: str
    revision: int
    package_sha256: str
    manifest: PluginManifest
    source_lock_status: str
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "plugin_id": self.plugin_id,
            "revision": self.revision,
            "package_sha256": self.package_sha256,
            "manifest": self.manifest.to_dict(),
            "source_lock_status": self.source_lock_status,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class PluginMcpConfigSource:
    """由已验证插件快照提供的 MCP 配置来源。

    该对象仅在 Python 运行时内部传递。任务 checkpoint 只冻结插件 ID、
    包哈希和配置哈希，绝不持久化本机快照的绝对路径。
    """

    plugin_id: str
    package_sha256: str
    config_path: Path


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

    def add_trust_key(self, key_id: str, public_key_base64: str) -> PluginTrustKey:
        """登记一个发布者公钥；同名 ID 不允许静默替换。"""

        public_key = _decode_public_key(public_key_base64)
        if not _TRUST_KEY_ID_PATTERN.fullmatch(key_id):
            raise PluginError("PLUGIN_TRUST_KEY_ID_INVALID", "可信公钥 ID 只能使用小写字母、数字、点、下划线和连字符。")
        normalized = base64.b64encode(public_key).decode("ascii")
        fingerprint = hashlib.sha256(public_key).hexdigest()
        existing = self._connection.execute("SELECT public_key_base64 FROM plugin_trust_keys WHERE key_id = ?", (key_id,)).fetchone()
        if existing is not None:
            if existing["public_key_base64"] == normalized:
                return self.trust_key(key_id)
            raise PluginError("PLUGIN_TRUST_KEY_ALREADY_EXISTS", "同名可信公钥已存在，不能静默替换发布者身份。")
        now = self._now()
        self._connection.execute(
            "INSERT INTO plugin_trust_keys(key_id, public_key_base64, fingerprint, created_at) VALUES (?, ?, ?, ?)",
            (key_id, normalized, fingerprint, now),
        )
        self._trust_audit(key_id, "PLUGIN_TRUST_KEY_ADDED", "READY", {"fingerprint": fingerprint})
        self._connection.commit()
        return self.trust_key(key_id)

    def trust_key(self, key_id: str) -> PluginTrustKey:
        row = self._connection.execute("SELECT * FROM plugin_trust_keys WHERE key_id = ?", (key_id,)).fetchone()
        if row is None:
            raise PluginError("PLUGIN_TRUST_KEY_NOT_FOUND", "未找到可信插件发布者公钥。")
        return PluginTrustKey(
            key_id=row["key_id"],
            public_key_base64=row["public_key_base64"],
            fingerprint=row["fingerprint"],
            created_at=row["created_at"],
        )

    def trust_keys(self) -> tuple[PluginTrustKey, ...]:
        rows = self._connection.execute("SELECT * FROM plugin_trust_keys ORDER BY key_id ASC").fetchall()
        return tuple(
            PluginTrustKey(
                key_id=row["key_id"],
                public_key_base64=row["public_key_base64"],
                fingerprint=row["fingerprint"],
                created_at=row["created_at"],
            )
            for row in rows
        )

    def remove_trust_key(self, key_id: str) -> bool:
        self.trust_key(key_id)
        cursor = self._connection.execute("DELETE FROM plugin_trust_keys WHERE key_id = ?", (key_id,))
        self._trust_audit(key_id, "PLUGIN_TRUST_KEY_REMOVED", "READY", {})
        self._connection.commit()
        return cursor.rowcount == 1

    def trust_audit(self, key_id: str | None = None, limit: int = 100) -> tuple[dict[str, object], ...]:
        if not 1 <= limit <= 500:
            raise PluginError("PLUGIN_AUDIT_LIMIT_INVALID", "审计查询条数必须在 1 到 500 之间。")
        if key_id is None:
            rows = self._connection.execute("SELECT * FROM plugin_trust_audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM plugin_trust_audit WHERE key_id = ? ORDER BY id DESC LIMIT ?", (key_id, limit)
            ).fetchall()
        return tuple(
            {
                "key_id": row["key_id"],
                "action": row["action"],
                "status": row["status"],
                "details": json.loads(row["details_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        )

    def install(self, source: Path) -> PluginRecord:
        source_root = _plugin_root(source)
        manifest = PluginManifest.load(source_root)
        if not manifest.is_compatible_with():
            raise PluginError(
                "PLUGIN_REPOPILOT_VERSION_INCOMPATIBLE",
                f"插件要求 RepoPilot {manifest.requires_repopilot}，当前版本为 {REPOPILOT_VERSION}。",
            )
        package_sha256 = _package_sha256(source_root)
        signature_status, signing_key_id = self._signature_status(source_root, package_sha256)
        # 来源锁本身属于被签名的声明。只有签名已通过可信发布者校验，才允许
        # 将其作为安装来源的可信依据；无签名或无效签名不能伪装成普通本地来源。
        source_lock_status = "UNVERIFIED"
        if signature_status == "VERIFIED":
            source_lock_status = _verify_source_lock(source_root, PluginSignature.load(source_root).source_lock)
        existing = self._connection.execute("SELECT * FROM plugins WHERE plugin_id = ?", (manifest.plugin_id,)).fetchone()
        if existing is not None and Path(existing["source_path"] or existing["root_path"]).resolve() != source_root:
            raise PluginError("PLUGIN_ID_ALREADY_INSTALLED", "同一插件 ID 已登记到其他目录；请先显式移除旧登记。")
        snapshot_root = self._snapshot_package(source_root, manifest.plugin_id, package_sha256)
        now = self._now()
        action = "PLUGIN_INSTALLED" if existing is None else "PLUGIN_REINSTALLED"
        installed_at = now if existing is None else existing["installed_at"]
        enabled = int(signature_status == "VERIFIED") if existing is None else existing["enabled"]
        self._connection.execute(
            """
            INSERT INTO plugins(plugin_id, source_path, root_path, manifest_json, package_sha256, enabled, source_lock_status, installed_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(plugin_id) DO UPDATE SET source_path=excluded.source_path, root_path=excluded.root_path,
                manifest_json=excluded.manifest_json, package_sha256=excluded.package_sha256, enabled=excluded.enabled,
                source_lock_status=excluded.source_lock_status,
                updated_at=excluded.updated_at
            """,
            (
                manifest.plugin_id,
                str(source_root),
                str(snapshot_root),
                _canonical_json(manifest.to_dict()),
                package_sha256,
                enabled,
                source_lock_status,
                installed_at,
                now,
            ),
        )
        self._record_version(manifest, source_root, snapshot_root, package_sha256, source_lock_status, now)
        self._audit(
            manifest.plugin_id,
            action,
            "READY" if signature_status == "VERIFIED" else "BLOCKED",
            {
                "package_sha256": package_sha256,
                "enabled": bool(enabled),
                "signature_status": signature_status,
                "signing_key_id": signing_key_id,
                "source_lock_status": source_lock_status,
            },
        )
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

    def versions(self, plugin_id: str) -> tuple[PluginVersion, ...]:
        self.get(plugin_id)
        rows = self._connection.execute(
            """
            SELECT plugin_id, revision, package_sha256, manifest_json, source_lock_status, created_at
            FROM plugin_versions WHERE plugin_id = ? ORDER BY revision DESC
            """,
            (plugin_id,),
        ).fetchall()
        return tuple(
            PluginVersion(
                plugin_id=row["plugin_id"],
                revision=int(row["revision"]),
                package_sha256=row["package_sha256"],
                manifest=_stored_manifest(row["manifest_json"]),
                source_lock_status=row["source_lock_status"] or "LOCAL_EXPLICIT",
                created_at=row["created_at"],
            )
            for row in rows
        )

    def rollback(self, plugin_id: str, package_sha256: str) -> PluginRecord:
        """显式切回已验证快照；不重新读取或执行原始安装目录。"""

        if not re.fullmatch(r"[0-9a-f]{64}", package_sha256):
            raise PluginError("PLUGIN_VERSION_HASH_INVALID", "插件版本哈希格式无效。")
        current = self.get(plugin_id)
        row = self._connection.execute(
            """
            SELECT source_path, root_path, manifest_json, package_sha256, source_lock_status
            FROM plugin_versions WHERE plugin_id = ? AND package_sha256 = ?
            """,
            (plugin_id, package_sha256),
        ).fetchone()
        if row is None:
            raise PluginError("PLUGIN_VERSION_NOT_FOUND", "未找到可回退的插件快照版本。")
        snapshot_root = Path(row["root_path"]).expanduser().resolve()
        manifest = _stored_manifest(row["manifest_json"])
        signature_status, signing_key_id = self._signature_status(snapshot_root, package_sha256)
        if (
            manifest.plugin_id != plugin_id
            or not manifest.is_compatible_with()
            or not _verified_snapshot(snapshot_root, package_sha256)
            or signature_status != "VERIFIED"
        ):
            self._audit(
                plugin_id,
                "PLUGIN_ROLLBACK_BLOCKED",
                "BLOCKED",
                {"package_sha256": package_sha256, "signature_status": signature_status, "signing_key_id": signing_key_id},
            )
            self._connection.commit()
            raise PluginError("PLUGIN_SNAPSHOT_INTEGRITY_FAILED", "目标插件快照不可读取、签名无效或完整性校验失败。")
        now = self._now()
        self._connection.execute(
            """
            UPDATE plugins
            SET source_path = ?, root_path = ?, manifest_json = ?, package_sha256 = ?, source_lock_status = ?, updated_at = ?
            WHERE plugin_id = ?
            """,
            (
                row["source_path"],
                str(snapshot_root),
                row["manifest_json"],
                package_sha256,
                row["source_lock_status"] or "LOCAL_EXPLICIT",
                now,
                plugin_id,
            ),
        )
        self._audit(
            plugin_id,
            "PLUGIN_ROLLED_BACK",
            "READY",
            {
                "from_package_sha256": current.package_sha256,
                "to_package_sha256": package_sha256,
                "source_lock_status": row["source_lock_status"] or "LOCAL_EXPLICIT",
            },
        )
        self._connection.commit()
        return self.get(plugin_id)

    def enable(self, plugin_id: str) -> PluginRecord:
        record = self.get(plugin_id)
        if record.integrity_status != "VERIFIED":
            self._audit(plugin_id, "PLUGIN_ENABLE_BLOCKED", "BLOCKED", {"integrity_status": record.integrity_status})
            self._connection.commit()
            raise PluginError("PLUGIN_INTEGRITY_CHECK_FAILED", "插件内容已变化或不可读取；请审查后重新安装。")
        if record.compatibility_status != "COMPATIBLE":
            self._audit(
                plugin_id,
                "PLUGIN_ENABLE_BLOCKED",
                "BLOCKED",
                {"compatibility_status": record.compatibility_status},
            )
            self._connection.commit()
            raise PluginError("PLUGIN_COMPATIBILITY_CHECK_FAILED", "插件不兼容当前 RepoPilot 版本；请升级插件或应用。")
        if record.signature_status != "VERIFIED":
            self._audit(
                plugin_id,
                "PLUGIN_ENABLE_BLOCKED",
                "BLOCKED",
                {"signature_status": record.signature_status, "signing_key_id": record.signing_key_id},
            )
            self._connection.commit()
            raise PluginError("PLUGIN_SIGNATURE_CHECK_FAILED", "插件签名未通过可信发布者校验，不能启用。")
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
            if (
                not record.enabled
                or record.integrity_status != "VERIFIED"
                or record.compatibility_status != "COMPATIBLE"
                or record.signature_status != "VERIFIED"
                or not record.manifest.skills_root
            ):
                continue
            candidate = (record.root_path / record.manifest.skills_root).resolve()
            if candidate.is_dir() and _is_within(candidate, record.root_path):
                roots.append(candidate)
        return tuple(roots)

    def active_mcp_configs(self) -> tuple[Path, ...]:
        """返回经过完整性校验的配置路径；调用方仍必须走 MCP 权限与 Schema 校验。"""

        return tuple(source.config_path for source in self.active_mcp_config_sources())

    def active_hooks(self) -> tuple[PluginHook, ...]:
        """只暴露已启用且全部信任条件仍成立的插件 Hook 声明。"""

        hooks: list[PluginHook] = []
        for record in self.list():
            if (
                record.enabled
                and record.integrity_status == "VERIFIED"
                and record.compatibility_status == "COMPATIBLE"
                and record.signature_status == "VERIFIED"
            ):
                hooks.extend(record.manifest.hooks)
        return tuple(sorted(hooks, key=lambda item: (item.event, item.plugin_id, item.hook_id)))

    def active_mcp_config_sources(self) -> tuple[PluginMcpConfigSource, ...]:
        """返回已启用且当前快照完整的插件 MCP 来源。

        不读取用户原始安装目录；只有受控快照通过全包哈希复验后才会进入
        任务绑定候选集。
        """

        sources: list[PluginMcpConfigSource] = []
        for record in self.list():
            if (
                not record.enabled
                or record.integrity_status != "VERIFIED"
                or record.compatibility_status != "COMPATIBLE"
                or record.signature_status != "VERIFIED"
                or not record.manifest.mcp_config
            ):
                continue
            candidate = (record.root_path / record.manifest.mcp_config).resolve()
            if candidate.is_file() and _is_within(candidate, record.root_path):
                sources.append(PluginMcpConfigSource(record.plugin_id, record.package_sha256, candidate))
        return tuple(sources)

    def mcp_config_source(
        self,
        plugin_id: str,
        expected_package_sha256: str,
    ) -> PluginMcpConfigSource | None:
        """按任务冻结的插件身份重新取得来源；版本、启用或完整性漂移即失效。"""

        if not _PLUGIN_ID_PATTERN.fullmatch(plugin_id) or not re.fullmatch(r"[0-9a-f]{64}", expected_package_sha256):
            return None
        try:
            record = self.get(plugin_id)
        except PluginError:
            return None
        if (
            not record.enabled
            or record.integrity_status != "VERIFIED"
            or record.compatibility_status != "COMPATIBLE"
            or record.signature_status != "VERIFIED"
            or record.package_sha256 != expected_package_sha256
            or not record.manifest.mcp_config
        ):
            return None
        candidate = (record.root_path / record.manifest.mcp_config).resolve()
        if not candidate.is_file() or not _is_within(candidate, record.root_path):
            return None
        return PluginMcpConfigSource(record.plugin_id, record.package_sha256, candidate)

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
        compatibility_status = "COMPATIBLE" if manifest.is_compatible_with() else "INCOMPATIBLE"
        signature_status, signing_key_id = self._signature_status(root, row["package_sha256"])
        return PluginRecord(
            plugin_id=row["plugin_id"],
            source_path=Path(row["source_path"] or row["root_path"]),
            root_path=root,
            manifest=manifest,
            package_sha256=row["package_sha256"],
            enabled=bool(row["enabled"]),
            integrity_status=integrity_status,
            compatibility_status=compatibility_status,
            signature_status=signature_status,
            signing_key_id=signing_key_id,
            source_lock_status=row["source_lock_status"] or "LOCAL_EXPLICIT",
            installed_at=row["installed_at"],
            updated_at=row["updated_at"],
        )

    def _signature_status(self, root: Path, package_sha256: str) -> tuple[str, str | None]:
        """每次从受控快照重新验签，撤销信任会立即收回运行时能力。"""

        try:
            signature = PluginSignature.load(root)
        except PluginError as error:
            if error.code == "PLUGIN_SIGNATURE_NOT_FOUND":
                return "UNSIGNED", None
            return "INVALID", None
        if signature.package_sha256 != package_sha256:
            return "INVALID", signature.key_id
        try:
            trust_key = self.trust_key(signature.key_id)
            public_key = Ed25519PublicKey.from_public_bytes(_decode_public_key(trust_key.public_key_base64))
            public_key.verify(base64.b64decode(signature.signature_base64, validate=True), signature.signed_payload())
        except PluginError:
            return "UNTRUSTED", signature.key_id
        except (InvalidSignature, ValueError, binascii.Error):
            return "INVALID", signature.key_id
        return "VERIFIED", signature.key_id

    def _record_version(
        self,
        manifest: PluginManifest,
        source_root: Path,
        snapshot_root: Path,
        package_sha256: str,
        source_lock_status: str,
        created_at: str,
    ) -> None:
        existing = self._connection.execute(
            "SELECT 1 FROM plugin_versions WHERE plugin_id = ? AND package_sha256 = ?",
            (manifest.plugin_id, package_sha256),
        ).fetchone()
        if existing is not None:
            return
        revision = int(
            self._connection.execute(
                "SELECT COALESCE(MAX(revision), 0) FROM plugin_versions WHERE plugin_id = ?",
                (manifest.plugin_id,),
            ).fetchone()[0]
        ) + 1
        self._connection.execute(
            """
            INSERT INTO plugin_versions(plugin_id, revision, source_path, root_path, manifest_json, package_sha256, source_lock_status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                manifest.plugin_id,
                revision,
                str(source_root),
                str(snapshot_root),
                _canonical_json(manifest.to_dict()),
                package_sha256,
                source_lock_status,
                created_at,
            ),
        )

    def _snapshot_package(self, source_root: Path, plugin_id: str, package_sha256: str) -> Path:
        destination = (self.database_path.parent / "plugin-cache" / plugin_id / package_sha256).resolve()
        cache_root = (self.database_path.parent / "plugin-cache").resolve()
        if not _is_within(destination, cache_root):
            raise PluginError("PLUGIN_SNAPSHOT_PATH_INVALID", "插件快照路径无效。")
        if destination.exists():
            if _verified_snapshot(destination, package_sha256):
                return destination
            raise PluginError("PLUGIN_SNAPSHOT_INTEGRITY_FAILED", "已有插件快照完整性校验失败。")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.parent / f".{package_sha256}.tmp-{uuid4().hex}"
        try:
            _copy_plugin_package(source_root, temporary)
            if not _verified_snapshot(temporary, package_sha256):
                raise PluginError("PLUGIN_SNAPSHOT_HASH_MISMATCH", "插件安装期间内容发生变化，未登记该快照。")
            os.replace(temporary, destination)
            return destination
        except OSError as error:
            raise PluginError("PLUGIN_SNAPSHOT_WRITE_FAILED", "插件快照无法安全写入本地状态目录。") from error
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    def _audit(self, plugin_id: str, action: str, status: str, details: dict[str, object]) -> None:
        self._connection.execute(
            "INSERT INTO plugin_audit(plugin_id, action, status, details_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (plugin_id, action, status, _canonical_json(details), self._now()),
        )

    def _trust_audit(self, key_id: str, action: str, status: str, details: dict[str, object]) -> None:
        self._connection.execute(
            "INSERT INTO plugin_trust_audit(key_id, action, status, details_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (key_id, action, status, _canonical_json(details), self._now()),
        )

    def _initialize(self) -> None:
        self._connection.executescript(
            """
                CREATE TABLE IF NOT EXISTS plugins (
                plugin_id TEXT PRIMARY KEY,
                source_path TEXT,
                root_path TEXT NOT NULL,
                manifest_json TEXT NOT NULL,
                package_sha256 TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                source_lock_status TEXT NOT NULL DEFAULT 'LOCAL_EXPLICIT',
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
                CREATE TABLE IF NOT EXISTS plugin_versions (
                    plugin_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    source_path TEXT NOT NULL,
                    root_path TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    package_sha256 TEXT NOT NULL,
                    source_lock_status TEXT NOT NULL DEFAULT 'LOCAL_EXPLICIT',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(plugin_id, revision),
                    UNIQUE(plugin_id, package_sha256)
                );
                CREATE INDEX IF NOT EXISTS idx_plugin_versions_plugin_revision ON plugin_versions(plugin_id, revision DESC);
                CREATE TABLE IF NOT EXISTS plugin_trust_keys (
                    key_id TEXT PRIMARY KEY,
                    public_key_base64 TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS plugin_trust_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_plugin_trust_audit_key_created ON plugin_trust_audit(key_id, created_at DESC);
                """
            )
        self._ensure_column("plugins", "source_path", "TEXT")
        self._ensure_column("plugins", "source_lock_status", "TEXT NOT NULL DEFAULT 'LOCAL_EXPLICIT'")
        self._ensure_column("plugin_versions", "source_lock_status", "TEXT NOT NULL DEFAULT 'LOCAL_EXPLICIT'")
        self._connection.execute("UPDATE plugins SET source_path = root_path WHERE source_path IS NULL OR source_path = ''")
        self._connection.execute("UPDATE plugins SET source_lock_status = 'LOCAL_EXPLICIT' WHERE source_lock_status IS NULL OR source_lock_status = ''")
        self._connection.execute("UPDATE plugin_versions SET source_lock_status = 'LOCAL_EXPLICIT' WHERE source_lock_status IS NULL OR source_lock_status = ''")
        self._connection.commit()

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        columns = {row["name"] for row in self._connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self._connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()


def sign_plugin_package(
    source: Path,
    key_id: str,
    private_key_file: Path,
    *,
    lock_current_git_source: bool = False,
) -> dict[str, str]:
    """生成 detached Ed25519 签名；私钥仅在当前进程短暂读取，绝不进入 SQLite 或审计。"""

    source_root = _plugin_root(source)
    PluginManifest.load(source_root)
    if not _TRUST_KEY_ID_PATTERN.fullmatch(key_id):
        raise PluginError("PLUGIN_TRUST_KEY_ID_INVALID", "签名发布者 ID 只能使用小写字母、数字、点、下划线和连字符。")
    requested_key_path = private_key_file.expanduser()
    if requested_key_path.is_symlink():
        raise PluginError("PLUGIN_PRIVATE_KEY_UNAVAILABLE", "Ed25519 私钥文件不可读取或不安全。")
    key_path = requested_key_path.resolve()
    if not key_path.is_file():
        raise PluginError("PLUGIN_PRIVATE_KEY_UNAVAILABLE", "Ed25519 私钥文件不可读取或不安全。")
    try:
        encoded_key = key_path.read_bytes()
    except OSError as error:
        raise PluginError("PLUGIN_PRIVATE_KEY_UNAVAILABLE", "Ed25519 私钥文件不可读取或不安全。") from error
    if len(encoded_key) > 32 * 1024:
        raise PluginError("PLUGIN_PRIVATE_KEY_INVALID", "Ed25519 私钥文件超过 32 KiB 安全上限。")
    try:
        private_key = serialization.load_pem_private_key(encoded_key, password=None)
    except (TypeError, ValueError) as error:
        raise PluginError("PLUGIN_PRIVATE_KEY_INVALID", "私钥必须是不带口令的 Ed25519 PEM 文件。") from error
    if not isinstance(private_key, Ed25519PrivateKey):
        raise PluginError("PLUGIN_PRIVATE_KEY_INVALID", "私钥必须是不带口令的 Ed25519 PEM 文件。")

    package_sha256 = _package_sha256(source_root)
    source_lock = _source_lock_from_current_git(source_root) if lock_current_git_source else None
    unsigned = PluginSignature(
        key_id=key_id,
        package_sha256=package_sha256,
        signature_base64="",
        source_lock=source_lock,
    )
    signature_base64 = base64.b64encode(private_key.sign(unsigned.signed_payload())).decode("ascii")
    signature_path = source_root / PLUGIN_SIGNATURE_NAME
    temporary = signature_path.with_name(f".{PLUGIN_SIGNATURE_NAME}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            _canonical_json(
                {
                    "schema_version": 1,
                    "algorithm": "ed25519",
                    "key_id": key_id,
                    "package_sha256": package_sha256,
                    **({"source_lock": source_lock.to_dict()} if source_lock else {}),
                    "signature": signature_base64,
                }
            ),
            encoding="utf-8",
        )
        os.replace(temporary, signature_path)
    except OSError as error:
        raise PluginError("PLUGIN_SIGNATURE_WRITE_FAILED", "插件签名文件无法安全写入。") from error
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)
    return {
        "key_id": key_id,
        "package_sha256": package_sha256,
        "signature_file": PLUGIN_SIGNATURE_NAME,
        "source_lock_status": "LOCKED" if source_lock else "LOCAL_EXPLICIT",
    }


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


def _optional_hooks(value: object, plugin_id: str) -> tuple[PluginHook, ...]:
    """解析无副作用的 Hook 声明，避免插件配置演变为解释型执行入口。"""

    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > 16:
        raise PluginError("PLUGIN_HOOKS_INVALID", "插件 hooks 必须是不超过 16 项的声明列表。")
    hooks: list[PluginHook] = []
    seen_ids: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item).difference({"id", "event", "decision", "message", "context"}):
            raise PluginError("PLUGIN_HOOKS_INVALID", "插件 Hook 只能包含 id、event、decision、message 和 context。")
        hook_id = item.get("id")
        event = item.get("event")
        decision = item.get("decision")
        message = item.get("message")
        if not isinstance(hook_id, str) or not _HOOK_ID_PATTERN.fullmatch(hook_id) or hook_id in seen_ids:
            raise PluginError("PLUGIN_HOOKS_INVALID", "插件 Hook ID 无效或重复。")
        if event not in _HOOK_EVENTS or decision not in _HOOK_DECISIONS:
            raise PluginError("PLUGIN_HOOKS_INVALID", "插件 Hook 事件或决策类型不受支持。")
        if not isinstance(message, str) or not message.strip() or len(message.strip()) > 512:
            raise PluginError("PLUGIN_HOOKS_INVALID", "插件 Hook message 必须是长度不超过 512 的非空文本。")
        raw_context = item.get("context", {})
        if (
            not isinstance(raw_context, dict)
            or len(raw_context) > 16
            or any(
                not isinstance(key, str)
                or not _HOOK_CONTEXT_KEY_PATTERN.fullmatch(key)
                or not isinstance(context_value, str)
                or len(context_value) > 256
                for key, context_value in raw_context.items()
            )
        ):
            raise PluginError("PLUGIN_HOOKS_INVALID", "插件 Hook context 必须是受限的短文本键值对。")
        seen_ids.add(hook_id)
        hooks.append(
            PluginHook(
                plugin_id=plugin_id,
                hook_id=hook_id,
                event=event,
                decision=decision,
                message=message.strip(),
                context={key: value for key, value in raw_context.items()},
            )
        )
    return tuple(hooks)


def _optional_version_constraint(value: object) -> str | None:
    """校验受限的 SemVer AND 约束，例如 `>=0.1.0,<0.2.0`。"""

    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 128:
        raise PluginError("PLUGIN_MANIFEST_INVALID", "requires_repopilot 必须是非空 SemVer 版本约束。")
    constraints = tuple(part.strip() for part in value.split(","))
    if not 1 <= len(constraints) <= 4 or any(not _VERSION_CONSTRAINT_PATTERN.fullmatch(part) for part in constraints):
        raise PluginError("PLUGIN_MANIFEST_INVALID", "requires_repopilot 仅支持逗号连接的 SemVer 比较，例如 >=0.1.0,<0.2.0。")
    return ",".join(constraints)


def _optional_source_lock(value: object) -> PluginSourceLock | None:
    """只允许公开、无凭据的 Git 远程 URL 和固定完整提交哈希。"""

    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"repository", "revision"}:
        raise PluginError("PLUGIN_SOURCE_LOCK_INVALID", "source_lock 必须仅包含 repository 和 revision。")
    repository = value.get("repository")
    revision = value.get("revision")
    if not isinstance(repository, str) or not isinstance(revision, str):
        raise PluginError("PLUGIN_SOURCE_LOCK_INVALID", "source_lock 的 repository 和 revision 必须是字符串。")
    normalized_repository = _normalize_repository_url(repository)
    normalized_revision = revision.strip().lower()
    if not _GIT_REVISION_PATTERN.fullmatch(normalized_revision):
        raise PluginError("PLUGIN_SOURCE_LOCK_INVALID", "source_lock.revision 必须是 40 或 64 位完整 Git 提交哈希。")
    return PluginSourceLock(normalized_repository, normalized_revision)


def _normalize_repository_url(value: str) -> str:
    candidate = value.strip()
    if not candidate or len(candidate) > 512 or any(character.isspace() for character in candidate):
        raise PluginError("PLUGIN_SOURCE_LOCK_INVALID", "source_lock.repository 必须是公开且无空白字符的 Git 地址。")
    if _GIT_SCP_URL_PATTERN.fullmatch(candidate):
        host, path = candidate[4:].split(":", 1)
        return f"ssh://git@{host.lower()}/{path.removesuffix('.git')}"
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"https", "ssh"} or not parsed.hostname or not parsed.path or parsed.password or parsed.query or parsed.fragment:
        raise PluginError("PLUGIN_SOURCE_LOCK_INVALID", "source_lock.repository 只支持无凭据的 https、ssh 或 git@host:path 地址。")
    if parsed.scheme == "https" and parsed.username:
        raise PluginError("PLUGIN_SOURCE_LOCK_INVALID", "source_lock.repository 不允许携带 HTTPS 用户信息。")
    if parsed.scheme == "ssh" and parsed.username not in {None, "git"}:
        raise PluginError("PLUGIN_SOURCE_LOCK_INVALID", "source_lock.repository 仅允许标准 git SSH 用户名。")
    authority = parsed.hostname.lower()
    try:
        port = parsed.port
    except ValueError as error:
        raise PluginError("PLUGIN_SOURCE_LOCK_INVALID", "source_lock.repository 的端口格式无效。") from error
    if port is not None:
        authority = f"{authority}:{port}"
    username = "git@" if parsed.scheme == "ssh" else ""
    return f"{parsed.scheme}://{username}{authority}/{parsed.path.lstrip('/').rstrip('/').removesuffix('.git')}"


def _parse_semver(value: str) -> tuple[int, int, int]:
    match = _SEMVER_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError("PLUGIN_REPOPILOT_VERSION_INVALID")
    return tuple(int(item) for item in match.groups())


def _matches_version_constraint(current: tuple[int, int, int], constraint: str) -> bool:
    match = _VERSION_CONSTRAINT_PATTERN.fullmatch(constraint)
    if match is None:
        raise ValueError("PLUGIN_VERSION_CONSTRAINT_INVALID")
    operator = match.group(1) or "=="
    target = tuple(int(item) for item in match.groups()[1:])
    return {
        ">=": current >= target,
        ">": current > target,
        "<=": current <= target,
        "<": current < target,
        "==": current == target,
    }[operator]


def _package_sha256(root: Path) -> str:
    root = root.resolve()
    entries: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if ".git" in path.parts or "__pycache__" in path.parts or path == root / PLUGIN_SIGNATURE_NAME:
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


def _copy_plugin_package(source_root: Path, destination: Path) -> None:
    """复制已通过预检的常规文件；复制期仍逐项拦截符号链接和超限输入。"""

    copied = 0
    for source in sorted(source_root.rglob("*"), key=lambda item: item.as_posix()):
        if ".git" in source.parts or "__pycache__" in source.parts:
            continue
        if source.is_symlink():
            raise PluginError("PLUGIN_SYMLINK_BLOCKED", "插件包不能包含符号链接。")
        if not source.is_file():
            continue
        copied += 1
        if copied > MAX_PLUGIN_FILES:
            raise PluginError("PLUGIN_FILE_LIMIT_REACHED", "插件文件数量超过安全上限。")
        if source.stat().st_size > MAX_PLUGIN_FILE_BYTES:
            raise PluginError("PLUGIN_FILE_TOO_LARGE", "插件包含超过 1 MiB 的文件。")
        relative = source.resolve().relative_to(source_root)
        target = (destination / relative).resolve()
        if not _is_within(target, destination.resolve()):
            raise PluginError("PLUGIN_PATH_ESCAPE", "插件文件不能离开快照目录。")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def _decode_public_key(value: str) -> bytes:
    if not isinstance(value, str) or len(value) > 128:
        raise PluginError("PLUGIN_TRUST_KEY_INVALID", "可信 Ed25519 公钥必须是 Base64 编码的原始 32 字节。")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise PluginError("PLUGIN_TRUST_KEY_INVALID", "可信 Ed25519 公钥不是合法 Base64。") from error
    if len(decoded) != 32:
        raise PluginError("PLUGIN_TRUST_KEY_INVALID", "可信 Ed25519 公钥必须是原始 32 字节。")
    try:
        Ed25519PublicKey.from_public_bytes(decoded)
    except ValueError as error:
        raise PluginError("PLUGIN_TRUST_KEY_INVALID", "可信 Ed25519 公钥不可用。") from error
    return decoded


def _verify_source_lock(source_root: Path, source_lock: PluginSourceLock | None) -> str:
    """验证来源时只调用固定 Git argv，绝不发起 fetch、pull 或任意 Shell。"""

    if source_lock is None:
        return "LOCAL_EXPLICIT"
    repository_root = _git_output(source_root, "rev-parse", "--show-toplevel")
    remote = _git_output(Path(repository_root), "remote", "get-url", "origin")
    revision = _git_output(Path(repository_root), "rev-parse", "HEAD").lower()
    try:
        normalized_remote = _normalize_repository_url(remote)
    except PluginError as error:
        raise PluginError("PLUGIN_SOURCE_LOCK_MISMATCH", "本地 Git 远程地址不满足插件来源锁定。") from error
    if normalized_remote != source_lock.repository or revision != source_lock.revision:
        raise PluginError("PLUGIN_SOURCE_LOCK_MISMATCH", "本地 Git 远程地址或提交与插件来源锁定不一致。")
    return "LOCKED"


def _source_lock_from_current_git(source_root: Path) -> PluginSourceLock:
    repository_root = _git_output(source_root, "rev-parse", "--show-toplevel")
    remote = _normalize_repository_url(_git_output(Path(repository_root), "remote", "get-url", "origin"))
    revision = _git_output(Path(repository_root), "rev-parse", "HEAD").lower()
    if not _GIT_REVISION_PATTERN.fullmatch(revision):
        raise PluginError("PLUGIN_SOURCE_LOCK_GIT_UNAVAILABLE", "无法获得可用于插件来源锁定的完整 Git 提交哈希。")
    return PluginSourceLock(remote, revision)


def _git_output(directory: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ("git", "-C", str(directory), *arguments),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
            **hidden_process_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PluginError("PLUGIN_SOURCE_LOCK_GIT_UNAVAILABLE", "无法调用 Git 验证插件来源锁定。") from error
    output = completed.stdout.strip()
    if completed.returncode != 0 or not output:
        raise PluginError("PLUGIN_SOURCE_LOCK_GIT_UNAVAILABLE", "无法调用 Git 验证插件来源锁定。")
    return output


def _verified_snapshot(root: Path, package_sha256: str) -> bool:
    try:
        return root.is_dir() and _package_sha256(root) == package_sha256
    except PluginError:
        return False


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
            requires_repopilot=_optional_version_constraint(payload.get("requires_repopilot")),
            hooks=_optional_hooks(payload.get("hooks"), str(payload["id"])),
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
