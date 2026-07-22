from __future__ import annotations

import unittest

from langchain_core.tools import StructuredTool

from repopilot_guard.permissions import FULL_ACCESS_CONFIRMATION, PermissionGrant, PermissionMode, PermissionSnapshot
from repopilot_guard.tool_runtime import ToolRuntime


class RuntimeFoundationTests(unittest.TestCase):
    def test_permission_snapshot_round_trip_preserves_task_authorization(self) -> None:
        grant = PermissionGrant(PermissionMode.FULL, FULL_ACCESS_CONFIRMATION)
        snapshot = PermissionSnapshot.create("task-001", grant, "worktree")

        restored = PermissionSnapshot.from_dict(snapshot.to_dict())

        self.assertEqual("task-001", restored.task_id)
        self.assertEqual(PermissionMode.FULL, restored.grant.mode)
        self.assertEqual("worktree", restored.workspace_mode)
        self.assertEqual("USER_GRANTED_FULL_ACCESS", restored.to_dict()["audit_code"])

    def test_permission_snapshot_rejects_invalid_workspace(self) -> None:
        with self.assertRaisesRegex(ValueError, "PERMISSION_SNAPSHOT_WORKSPACE_INVALID"):
            PermissionSnapshot.from_dict(
                {
                    "task_id": "task-001",
                    "mode": "safe",
                    "confirmation": None,
                    "workspace_mode": "outside",
                    "granted_at": "2026-07-19T00:00:00+00:00",
                }
            )

    def test_tool_runtime_rejects_unknown_and_invalid_arguments(self) -> None:
        def echo(value: str) -> dict[str, object]:
            return {"status": "READY", "code": "ECHO", "message": value, "data": {"value": value}}

        runtime = ToolRuntime((StructuredTool.from_function(echo, name="echo", description="测试工具。"),))

        self.assertEqual("TOOL_NOT_ALLOWLISTED", runtime.invoke("shell", {}).code)
        self.assertEqual("INVALID_TOOL_ARGUMENTS", runtime.invoke("echo", {}).code)
        self.assertEqual("ECHO", runtime.invoke("echo", {"value": "ok"}).code)

    def test_tool_runtime_rejects_duplicate_registration(self) -> None:
        def first() -> dict[str, object]:
            return {}

        def second() -> dict[str, object]:
            return {}

        with self.assertRaisesRegex(ValueError, "DUPLICATE_TOOL_REGISTRATION"):
            ToolRuntime(
                (
                    StructuredTool.from_function(first, name="duplicate", description="测试工具。"),
                    StructuredTool.from_function(second, name="duplicate", description="测试工具。"),
                )
            )
