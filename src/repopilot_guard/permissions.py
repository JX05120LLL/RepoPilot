"""任务级权限模式与完全权限确认。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any


FULL_ACCESS_CONFIRMATION = "我已了解完全权限风险"


class PermissionMode(str, Enum):
    """用户为单个任务选择的工具权限范围。"""

    SAFE = "safe"
    FULL = "full"


@dataclass(frozen=True, slots=True)
class PermissionGrant:
    """已验证的任务级授权，不会自动继承到其他任务。"""

    mode: PermissionMode
    confirmation: str | None = None

    def __post_init__(self) -> None:
        if self.mode is PermissionMode.FULL and self.confirmation != FULL_ACCESS_CONFIRMATION:
            raise ValueError("FULL_ACCESS_CONFIRMATION_REQUIRED")

    @property
    def is_full_access(self) -> bool:
        return self.mode is PermissionMode.FULL

    @classmethod
    def safe(cls) -> "PermissionGrant":
        return cls(PermissionMode.SAFE)

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "full_access_confirmed": self.is_full_access,
            "audit_code": "USER_GRANTED_FULL_ACCESS" if self.is_full_access else "SAFE_MODE",
        }


@dataclass(frozen=True, slots=True)
class PermissionSnapshot:
    """任务创建时冻结的权限上下文，供 checkpoint 恢复时校验。"""

    task_id: str
    grant: PermissionGrant
    workspace_mode: str
    granted_at: str
    approved_mcp_tools: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("PERMISSION_SNAPSHOT_TASK_ID_REQUIRED")
        if self.workspace_mode not in {"local", "worktree"}:
            raise ValueError("PERMISSION_SNAPSHOT_WORKSPACE_INVALID")
        if len(self.approved_mcp_tools) > 64 or any(
            not isinstance(capability_id, str) or not capability_id.startswith("mcp__")
            for capability_id in self.approved_mcp_tools
        ):
            raise ValueError("PERMISSION_SNAPSHOT_MCP_APPROVAL_INVALID")
        try:
            datetime.fromisoformat(self.granted_at)
        except ValueError as error:
            raise ValueError("PERMISSION_SNAPSHOT_TIME_INVALID") from error

    @classmethod
    def create(
        cls,
        task_id: str,
        grant: PermissionGrant,
        workspace_mode: str,
        approved_mcp_tools: tuple[str, ...] = (),
    ) -> "PermissionSnapshot":
        return cls(
            task_id=task_id,
            grant=grant,
            workspace_mode=workspace_mode,
            granted_at=datetime.now(timezone.utc).isoformat(),
            approved_mcp_tools=tuple(sorted(set(approved_mcp_tools))),
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PermissionSnapshot":
        try:
            mode = PermissionMode(str(payload["mode"]))
            confirmation = payload.get("confirmation")
            if confirmation is not None and not isinstance(confirmation, str):
                raise ValueError("PERMISSION_SNAPSHOT_CONFIRMATION_INVALID")
            return cls(
                task_id=str(payload["task_id"]),
                grant=PermissionGrant(mode, confirmation),
                workspace_mode=str(payload["workspace_mode"]),
                granted_at=str(payload["granted_at"]),
                approved_mcp_tools=tuple(sorted(set(_mcp_tool_ids(payload.get("approved_mcp_tools", []))))),
            )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, ValueError) and str(error).startswith("PERMISSION_SNAPSHOT_"):
                raise
            raise ValueError("PERMISSION_SNAPSHOT_INVALID") from error

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "mode": self.grant.mode.value,
            "confirmation": self.grant.confirmation,
            "workspace_mode": self.workspace_mode,
            "granted_at": self.granted_at,
            "approved_mcp_tools": list(self.approved_mcp_tools),
            "audit_code": "USER_GRANTED_FULL_ACCESS" if self.grant.is_full_access else "SAFE_MODE",
        }


def _mcp_tool_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("PERMISSION_SNAPSHOT_MCP_APPROVAL_INVALID")
    return tuple(value)
