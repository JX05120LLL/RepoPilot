from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from repopilot_guard.capabilities import CapabilityPolicy, CapabilityRegistry
from repopilot_guard.permissions import FULL_ACCESS_CONFIRMATION, PermissionGrant, PermissionMode
from repopilot_guard.shell_runtime import ShellCommandRequest, ShellRuntime, shell_capability
from repopilot_guard.tool_runtime import ToolRuntime


class ShellRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self._temporary_directory.name) / "project"
        self.workspace.mkdir()
        self.full_permission = PermissionGrant(PermissionMode.FULL, FULL_ACCESS_CONFIRMATION)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_shell_is_disabled_by_default_even_with_full_permission(self) -> None:
        result = ShellRuntime().run(
            self.workspace,
            ShellCommandRequest(argv=[sys.executable, "--version"]),
            self.full_permission,
            capability_approved=True,
        )

        self.assertEqual("BLOCKED", result.status)
        self.assertEqual("SHELL_FEATURE_DISABLED", result.code)

    def test_shell_requires_full_permission_and_task_capability_approval(self) -> None:
        request = ShellCommandRequest(argv=[sys.executable, "--version"])
        runtime = ShellRuntime(enabled=True)

        safe = runtime.run(self.workspace, request, PermissionGrant.safe(), capability_approved=True)
        waiting = runtime.run(self.workspace, request, self.full_permission)

        self.assertEqual("SHELL_FULL_LOCAL_REQUIRED", safe.code)
        self.assertEqual("SHELL_CAPABILITY_APPROVAL_REQUIRED", waiting.code)

    def test_preview_never_starts_a_process_and_marks_execution_risk(self) -> None:
        runtime = ShellRuntime(enabled=True)
        target = self.workspace / "must-not-exist.txt"
        preview = runtime.preview(
            self.workspace,
            ShellCommandRequest(
                argv=[sys.executable, "-c", f"open(r'{target}', 'w').write('unexpected')"],
                timeout_seconds=17,
            ),
            self.full_permission,
            capability_approved=True,
        )

        self.assertEqual("READY", preview.status)
        self.assertEqual("SHELL_PREVIEW_READY", preview.code)
        self.assertTrue(preview.requires_execution_approval)
        self.assertIn("write", preview.risk_categories)
        self.assertEqual(17, preview.timeout_seconds)
        self.assertFalse(target.exists())

    def test_preview_allows_external_path_in_full_local_mode(self) -> None:
        runtime = ShellRuntime(enabled=True)
        preview = runtime.preview(
            self.workspace,
            ShellCommandRequest(argv=[sys.executable, "../outside.py"]),
            self.full_permission,
            capability_approved=True,
        )

        self.assertEqual("READY", preview.status)
        self.assertIn("write", preview.risk_categories)

    def test_shell_executes_direct_argv_with_project_cwd_and_minimal_environment(self) -> None:
        runtime = ShellRuntime(enabled=True, environment={"PATH": os.environ.get("PATH", ""), "REPOPILOT_CHAT_API_KEY": "must-not-leak"})
        request = ShellCommandRequest(
            argv=[sys.executable, "-c", "import os; print(os.getcwd()); print(os.getenv('REPOPILOT_CHAT_API_KEY'))"],
        )

        result = runtime.run(self.workspace, request, self.full_permission, capability_approved=True, risk_approved=True)

        self.assertEqual("READY", result.status)
        self.assertEqual("SHELL_SUCCEEDED", result.code)
        self.assertIn(str(self.workspace), result.stdout_summary)
        self.assertIn("None", result.stdout_summary)
        self.assertNotIn("REPOPILOT_CHAT_API_KEY", result.environment_names)

    def test_shell_allows_interpreter_and_git_delivery_commands_after_risk_approval(self) -> None:
        runtime = ShellRuntime(enabled=True)
        host = runtime.run(
            self.workspace,
            ShellCommandRequest(argv=["cmd.exe", "/c", "echo full-local-shell"]),
            self.full_permission,
            capability_approved=True,
            risk_approved=True,
        )
        subprocess.run(["git", "init"], cwd=self.workspace, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.email", "repopilot@example.test"], cwd=self.workspace, check=True)
        subprocess.run(["git", "config", "user.name", "RepoPilot Test"], cwd=self.workspace, check=True)
        (self.workspace / "README.md").write_text("delivery\n", encoding="utf-8")
        added = runtime.run(
            self.workspace,
            ShellCommandRequest(argv=["git", "add", "README.md"]),
            self.full_permission,
            capability_approved=True,
            risk_approved=True,
        )
        committed = runtime.run(
            self.workspace,
            ShellCommandRequest(argv=["git", "commit", "-m", "RepoPilot test delivery"]),
            self.full_permission,
            capability_approved=True,
            risk_approved=True,
        )
        remote = self.workspace.parent / "remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True, text=True)
        subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=self.workspace, check=True)
        pushed = runtime.run(
            self.workspace,
            ShellCommandRequest(argv=["git", "push", "origin", "HEAD"]),
            self.full_permission,
            capability_approved=True,
            risk_approved=True,
        )

        self.assertEqual("READY", host.status)
        self.assertIn("shell_interpreter", runtime.preview(self.workspace, ShellCommandRequest(argv=["cmd.exe", "/c", "echo full-local-shell"]), self.full_permission, capability_approved=True).risk_categories)
        self.assertEqual("READY", added.status)
        self.assertEqual("READY", committed.status)
        self.assertEqual("READY", pushed.status)

    def test_shell_requires_extra_approval_for_network_or_package_commands(self) -> None:
        runtime = ShellRuntime(enabled=True)
        request = ShellCommandRequest(argv=["npm", "install"])

        waiting = runtime.run(self.workspace, request, self.full_permission, capability_approved=True)
        approved = runtime.run(
            self.workspace,
            request,
            self.full_permission,
            capability_approved=True,
            risk_approved=True,
        )

        self.assertEqual("SHELL_NETWORK_APPROVAL_REQUIRED", waiting.code)
        # 本机未必安装 npm；该断言只验证额外审批已通过策略，不要求命令成功。
        self.assertNotEqual("SHELL_NETWORK_APPROVAL_REQUIRED", approved.code)

    def test_shell_redacts_secret_like_output_and_exposes_capability_metadata(self) -> None:
        runtime = ShellRuntime(enabled=True)
        request = ShellCommandRequest(
            argv=[sys.executable, "-c", "print('token=visible-secret'); print('authorization: Bearer second-secret')"]
        )

        result = runtime.run(self.workspace, request, self.full_permission, capability_approved=True, risk_approved=True)
        descriptor = shell_capability()

        self.assertEqual("READY", result.status)
        self.assertIn("token=[REDACTED]", result.stdout_summary)
        self.assertIn("authorization: [REDACTED]", result.stdout_summary)
        self.assertNotIn("visible-secret", result.stdout_summary)
        self.assertNotIn("second-secret", result.stdout_summary)
        self.assertEqual("shell", descriptor.capability_id)
        self.assertIn("process", {risk.value for risk in descriptor.risks})
        self.assertTrue(descriptor.requires_approval)

    def test_tool_runtime_requires_shell_capability_approval_even_in_full_local_mode(self) -> None:
        runtime = ShellRuntime(enabled=True)
        tool = runtime.as_structured_tool(
            self.workspace,
            self.full_permission,
            capability_approved=False,
        )
        denied = ToolRuntime(
            (tool,),
            capabilities=CapabilityRegistry((shell_capability(),)),
            permission=self.full_permission,
        ).invoke("shell", {"argv": [sys.executable, "--version"]})
        allowed = ToolRuntime(
            (
                runtime.as_structured_tool(
                    self.workspace,
                    self.full_permission,
                    capability_approved=True,
                    read_only_only=True,
                ),
            ),
            capabilities=CapabilityRegistry((shell_capability(),)),
            permission=self.full_permission,
            approved_capabilities=("shell",),
            capability_policy=CapabilityPolicy(),
        ).invoke("shell", {"argv": [sys.executable, "--version"]})

        self.assertEqual("CAPABILITY_APPROVAL_REQUIRED", denied.code)
        self.assertEqual("READY", allowed.status)
        self.assertEqual("SHELL_SUCCEEDED", allowed.code)

    def test_research_shell_only_allows_recognized_read_only_commands(self) -> None:
        runtime = ShellRuntime(enabled=True)
        read_only = runtime.as_structured_tool(
            self.workspace,
            self.full_permission,
            capability_approved=True,
            read_only_only=True,
        )

        blocked = read_only.invoke({"argv": [sys.executable, "-c", "open('changed.txt', 'w').write('x')"]})
        version = read_only.invoke({"argv": [sys.executable, "--version"]})

        self.assertEqual("SHELL_EXECUTION_APPROVAL_REQUIRED", blocked["code"])
        self.assertEqual("SHELL_SUCCEEDED", version["code"])
        self.assertEqual("SHELL_PREVIEW_READY", version["preview"]["code"])
        self.assertEqual("read", version["preview"]["risk_categories"][-1])
        self.assertFalse((self.workspace / "changed.txt").exists())

    def test_non_read_only_structured_tool_returns_preview_without_running(self) -> None:
        runtime = ShellRuntime(enabled=True)
        tool = runtime.as_structured_tool(
            self.workspace,
            self.full_permission,
            capability_approved=True,
        )
        result = tool.invoke({"argv": [sys.executable, "-c", "open('blocked.txt', 'w').write('x')"]})

        self.assertEqual("BLOCKED", result["status"])
        self.assertEqual("SHELL_EXECUTION_APPROVAL_REQUIRED", result["code"])
        self.assertEqual("SHELL_PREVIEW_READY", result["preview"]["code"])
        self.assertFalse((self.workspace / "blocked.txt").exists())

    def test_shell_cancellation_terminates_the_managed_process(self) -> None:
        runtime = ShellRuntime(enabled=True)
        request = ShellCommandRequest(argv=[sys.executable, "-c", "import time; time.sleep(30)"], timeout_seconds=10)
        checks = 0

        def cancelled() -> bool:
            nonlocal checks
            checks += 1
            return checks >= 2

        result = runtime.run(
            self.workspace,
            request,
            self.full_permission,
            capability_approved=True,
            risk_approved=True,
            cancellation_requested=cancelled,
        )

        self.assertEqual("BLOCKED", result.status)
        self.assertEqual("SHELL_CANCELLED", result.code)
        self.assertIn("TERMINAT", result.process_tree_summary)


if __name__ == "__main__":
    unittest.main()
