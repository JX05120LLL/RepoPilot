from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
import zipfile
from io import StringIO
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

from repopilot_guard import __version__
from repopilot_guard.cli import (
    _desktop_backend_delivery_check,
    _desktop_command_version,
    _start_cli_lease_heartbeat,
    _task_summary,
    _windows_build_tool_candidates,
    main,
)
from repopilot_guard.config import ComponentCheck
from repopilot_guard.context import ManagedDocumentStore
from repopilot_guard.models import TaskBudget
from repopilot_guard.permissions import FULL_ACCESS_CONFIRMATION
from repopilot_guard.project_registry import ProjectRegistry
from repopilot_guard.task_store import TaskStore


def _initialize_git_repository(repository: Path) -> None:
    """为 CLI 任务测试创建真实 Git 基线。"""

    commands = (
        ("init", "-b", "main"),
        ("config", "user.name", "RepoPilot Test"),
        ("config", "user.email", "test@example.invalid"),
    )
    for arguments in commands:
        subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)
    (repository / "README.md").write_text("# fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=repository, check=True, capture_output=True)


class _Result:
    def __init__(
        self,
        *,
        status: str = "WAITING_APPROVAL",
        pending_approval: bool = True,
        thread_id: str = "thread-cli-1",
    ) -> None:
        self.status = status
        self._payload = {
            "thread_id": thread_id,
            "task_id": "task-cli-1",
            "status": status,
            "pending_approval": pending_approval,
            "verdict": None,
            "interrupts": [{"type": "PLAN_APPROVAL_REQUIRED", "message": "原始中断文案不得出现在摘要中"}] if pending_approval else [],
            "state": {
                "workspace_mode": "worktree",
                "workspace_path": "C:/temp/worktree",
                "base_commit": "abc123",
                "permission_mode": "safe",
                "messages": [{"role": "user", "content": "敏感任务正文不得出现在摘要中"}],
                "tool_events": [{"type": "PLAN_GENERATED", "arguments": {"secret": "不得输出"}}],
                "plan": {
                    "summary": "修复订单租户过滤。",
                "candidate_files": ["OrderService.java"],
                "steps": ["补充租户过滤。"],
                "verification": ["运行 Maven 测试。"],
                "verification_recipe": "test",
                },
                "verification_result": None,
            },
        }

    def to_dict(self) -> dict[str, object]:
        return self._payload


class CliProductTests(unittest.TestCase):
    def test_desktop_preview_uses_fixed_powershell_arguments(self) -> None:
        completed = SimpleNamespace(returncode=0)
        with (
            patch("repopilot_guard.cli.shutil.which", side_effect=lambda value: "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe" if value == "powershell.exe" else None),
            patch("repopilot_guard.cli.subprocess.run", return_value=completed) as run,
            patch("sys.stdout", StringIO()),
        ):
            exit_code = main(["desktop", "preview", "--port", "8767", "--ui-port", "1427"])

        self.assertEqual(0, exit_code)
        arguments = run.call_args.args[0]
        self.assertEqual("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe", arguments[0])
        self.assertEqual(("-NoProfile", "-ExecutionPolicy", "Bypass", "-File"), arguments[1:5])
        self.assertEqual(("-ApiPort", "8767", "-UiPort", "1427"), arguments[-4:])
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_desktop_preview_rejects_invalid_port_before_launching_process(self) -> None:
        with patch("repopilot_guard.cli.subprocess.run") as run, self.assertRaises(SystemExit):
            main(["desktop", "preview", "--port", "70000"])

        run.assert_not_called()

    def test_preview_script_rejects_occupied_ports_and_requires_repopilot_health_contract(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        script = (repository_root / "scripts" / "start-desktop-preview.ps1").read_text(encoding="utf-8")

        self.assertIn("Get-NetTCPConnection -LocalPort $ApiPort -State Listen", script)
        self.assertIn("Get-NetTCPConnection -LocalPort $UiPort -State Listen", script)
        self.assertIn("Test-RepoPilotHealth", script)
        self.assertIn("/api/health", script)
        self.assertIn('scope -eq "127.0.0.1-only"', script)
        self.assertIn('VITE_REPOPILOT_API_URL = "http://127.0.0.1:${ApiPort}/api"', script)
        self.assertIn('REPOPILOT_DESKTOP_PREVIEW_ORIGIN = "http://127.0.0.1:${UiPort}"', script)
        self.assertNotIn("Test-NetConnection", script)

    def test_welcome_guides_first_time_user_to_register_a_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_path = Path(temporary_directory) / "state.sqlite"
            output = StringIO()
            with patch("sys.stdout", output):
                exit_code = main(["welcome", "--state-db", str(state_path)])

        payload = json.loads(output.getvalue())
        self.assertEqual(0, exit_code)
        self.assertEqual("WELCOME_PROJECT_REQUIRED", payload["code"])
        self.assertEqual("REGISTER_PROJECT", payload["next_action"]["type"])
        self.assertIn("project add", payload["next_action"]["command"])

    def test_welcome_recommends_safe_task_without_exposing_project_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state_path = root / "state.sqlite"
            repository = root / "order-service"
            repository.mkdir()
            registry = ProjectRegistry(state_path)
            project = registry.add(repository, "订单服务")
            registry.close()
            output = StringIO()
            diagnosis = {
                "project": project.to_dict(),
                "recommended_task_mode": "safe-isolated",
                "task_modes": {
                    "safe_isolated": {"status": "READY", "code": "SAFE_ISOLATED_READY"},
                    "full_local": {"status": "READY", "code": "FULL_LOCAL_READY"},
                },
                "profiles": {"java_maven": {"status": "READY", "code": "JAVA_MAVEN_PROFILE_READY"}},
            }
            with (
                patch("repopilot_guard.cli.diagnose_project", return_value=diagnosis),
                patch("sys.stdout", output),
            ):
                exit_code = main(["welcome", "--state-db", str(state_path)])

        payload = json.loads(output.getvalue())
        self.assertEqual(0, exit_code)
        self.assertEqual("WELCOME_READY", payload["code"])
        self.assertEqual("START_SAFE_ISOLATED_TASK", payload["next_action"]["type"])
        self.assertIn(project.project_id, payload["next_action"]["command"])
        self.assertNotIn(str(repository), json.dumps(payload, ensure_ascii=False))

    def test_welcome_recommends_full_local_research_for_non_git_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state_path = root / "state.sqlite"
            repository = root / "plain-project"
            repository.mkdir()
            registry = ProjectRegistry(state_path)
            project = registry.add(repository, "普通目录")
            registry.close()
            output = StringIO()
            with patch("sys.stdout", output):
                exit_code = main(["welcome", "--state-db", str(state_path)])

        payload = json.loads(output.getvalue())
        command = payload["next_action"]["command"]
        self.assertEqual(0, exit_code)
        self.assertEqual("START_FULL_LOCAL_RESEARCH", payload["next_action"]["type"])
        self.assertEqual("research", payload["selected_project"]["recommended_task_operation"])
        self.assertEqual(["research"], payload["selected_project"]["full_local"]["allowed_operations"])
        self.assertIn(project.project_id, command)
        self.assertIn("--operation research", command)
        self.assertIn("--task-mode full-local", command)
        self.assertIn(FULL_ACCESS_CONFIRMATION, command)
        self.assertNotIn(str(repository), json.dumps(payload, ensure_ascii=False))

    def test_cli_prints_package_version_without_loading_runtime_configuration(self) -> None:
        output = StringIO()
        with patch("sys.stdout", output), self.assertRaises(SystemExit) as exit_context:
            main(["--version"])

        self.assertEqual(0, exit_context.exception.code)
        self.assertEqual(f"RepoPilot {__version__}\n", output.getvalue())

    def test_tauri_configuration_restricts_content_and_loopback_connections(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        config = json.loads((repository_root / "desktop" / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))

        csp = config["app"]["security"]["csp"]
        self.assertIsInstance(csp, str)
        self.assertIn("default-src 'self'", csp)
        self.assertIn("http://127.0.0.1:8765", csp)
        self.assertIn("ws://127.0.0.1:1420", csp)
        self.assertNotIn("http://0.0.0.0", csp)
        self.assertNotIn("https:", csp)
        self.assertIn("binaries/repopilot-guard.exe", config["bundle"]["resources"])
        self.assertEqual(["nsis"], config["bundle"]["targets"])
        self.assertTrue((repository_root / "desktop" / "src-tauri" / "icons" / "icon.ico").is_file())

        package = json.loads((repository_root / "desktop" / "package.json").read_text(encoding="utf-8"))
        self.assertIn("build-desktop-backend-sidecar.ps1", package["scripts"]["sidecar:build"])
        self.assertIn("npm run sidecar:build", package["scripts"]["tauri:build"])

        rust_source = (repository_root / "desktop" / "src-tauri" / "src" / "main.rs").read_text(encoding="utf-8")
        self.assertIn("app_data_dir", rust_source)
        self.assertIn("REPOPILOT_STATE_DB_PATH", rust_source)
        self.assertIn("REPOPILOT_DESKTOP_DATA_DIR", rust_source)
        self.assertIn("CREATE_NO_WINDOW", rust_source)
        self.assertIn("WindowEvent::CloseRequested", rust_source)
        self.assertIn("app_handle.exit(0)", rust_source)
        self.assertIn("tauri_plugin_single_instance", rust_source)
        self.assertIn("get_webview_window(\"main\")", rust_source)

    def test_task_summary_excludes_raw_messages_and_tool_arguments(self) -> None:
        result = _Result()
        result._payload["state"]["attached_documents"] = [
            {
                "document_id": "a" * 64,
                "display_name": "requirements.md",
                "content_sha256": "b" * 64,
                "source_path": "C:/private/requirements.md",
                "content": "不得输出的研发文档正文",
            }
        ]
        summary = _task_summary(result)
        encoded = json.dumps(summary, ensure_ascii=False)

        self.assertIn("PLAN_GENERATED", encoded)
        self.assertIn("OrderService.java", encoded)
        self.assertNotIn("敏感任务正文不得出现在摘要中", encoded)
        self.assertNotIn("不得输出", encoded)
        self.assertNotIn("原始中断文案不得出现在摘要中", encoded)
        self.assertNotIn("C:/private", encoded)
        self.assertNotIn("不得输出的研发文档正文", encoded)
        self.assertNotIn("interrupts", summary)
        self.assertEqual("plan_approval", summary["progress"]["current_stage"])
        self.assertEqual("current", summary["progress"]["stages"][4]["state"])
        self.assertEqual("requirements.md", summary["attached_documents"][0]["display_name"])
        self.assertEqual("PLAN_REVIEW", summary["approval"]["stage"])
        self.assertEqual(["OrderService.java"], summary["approval"]["candidate_files"])
        self.assertEqual("test", summary["approval"]["verification_recipe"])
        self.assertFalse(summary["approval"]["write_after_approval"])
        self.assertEqual(["approve", "revise", "reject"], summary["approval"]["allowed_decisions"])
        self.assertEqual("请审阅计划范围；确认计划本身不会写入代码。", summary["approval"]["summary"])

    def test_task_start_uses_safe_isolated_defaults_and_prints_next_action(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state_path = root / "state.sqlite"
            repository = root / "repository"
            repository.mkdir()
            _initialize_git_repository(repository)
            checkpoint = SimpleNamespace(checkpointer=object(), close=Mock())
            runner = Mock()
            runner.run.side_effect = lambda _request, thread_id, _permission: _Result(thread_id=thread_id)
            settings = SimpleNamespace(task_budget=lambda: TaskBudget())
            output = StringIO()
            with (
                patch("repopilot_guard.cli.AppSettings", return_value=settings),
                patch("repopilot_guard.cli.SqliteCheckpointStore", return_value=checkpoint),
                patch("repopilot_guard.cli.create_live_graph", return_value=object()),
                patch("repopilot_guard.cli.GraphRunner", return_value=runner),
                patch("sys.stdout", output),
            ):
                exit_code = main(
                    [
                        "task",
                        "start",
                        "--repo",
                        str(repository),
                        "--task",
                        "分析订单权限",
                        "--state-db",
                        str(state_path),
                        "--output",
                        str(root / "runs"),
                    ]
                )

            self.assertEqual(0, exit_code)
            request = runner.run.call_args.args[0]
            permission = runner.run.call_args.args[2]
            self.assertEqual("worktree", request.workspace_selection.mode.value)
            self.assertEqual("safe", permission.mode.value)
            payload = json.loads(output.getvalue())
            self.assertEqual("PLAN_APPROVAL_REQUIRED", payload["next_action"]["type"])
            self.assertIn("task decide", payload["next_action"]["command"])
            checkpoint.close.assert_called_once()
            task_store = TaskStore(state_path)
            try:
                stored = task_store.get(runner.run.call_args.args[1])
                artifacts = {item.kind for item in task_store.artifacts(stored.thread_id)}
            finally:
                task_store.close()
            self.assertEqual("WAITING_APPROVAL", stored.status)
            self.assertIsNone(stored.lease_expires_at)
            self.assertIn("plan_json", artifacts)

    def test_task_start_binds_only_registered_project_document(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state_path = root / "state.sqlite"
            repository = root / "repository"
            repository.mkdir()
            _initialize_git_repository(repository)
            source = root / "requirements.md"
            source.write_text("# 订单需求\n按租户隔离。\n", encoding="utf-8")
            registry = ProjectRegistry(state_path)
            project = registry.add(repository, "订单项目")
            registry.close()
            managed = ManagedDocumentStore(state_path).import_document(source, project_id=project.project_id)
            checkpoint = SimpleNamespace(checkpointer=object(), close=Mock())
            runner = Mock()
            runner.run.side_effect = lambda _request, thread_id, _permission: _Result(thread_id=thread_id)
            settings = SimpleNamespace(task_budget=lambda: TaskBudget())
            output = StringIO()
            with (
                patch("repopilot_guard.cli.AppSettings", return_value=settings),
                patch("repopilot_guard.cli.SqliteCheckpointStore", return_value=checkpoint),
                patch("repopilot_guard.cli.create_live_graph", return_value=object()),
                patch("repopilot_guard.cli.GraphRunner", return_value=runner),
                patch("sys.stdout", output),
            ):
                exit_code = main(
                    [
                        "task",
                        "start",
                        "--project-id",
                        project.project_id,
                        "--task",
                        "依据研发文档分析订单权限",
                        "--document-id",
                        managed.document_id,
                        "--state-db",
                        str(state_path),
                        "--output",
                        str(root / "runs"),
                    ]
                )

            self.assertEqual(0, exit_code)
            self.assertEqual((managed.document_id,), runner.run.call_args.args[0].attached_document_ids)
            self.assertNotIn(str(source), output.getvalue())

    def test_task_start_is_visible_with_lease_before_runner_returns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state_path = root / "state.sqlite"
            repository = root / "repository"
            repository.mkdir()
            _initialize_git_repository(repository)
            checkpoint = SimpleNamespace(checkpointer=object(), close=Mock())
            settings = SimpleNamespace(task_budget=lambda: TaskBudget())
            observed: dict[str, object] = {}

            def inspect_during_run(_request: object, thread_id: str, _permission: object) -> _Result:
                observer = TaskStore(state_path)
                try:
                    task = observer.get(thread_id)
                    observed["status"] = task.status
                    observed["lease"] = task.lease_expires_at
                finally:
                    observer.close()
                return _Result(thread_id=thread_id)

            runner = Mock()
            runner.run.side_effect = inspect_during_run
            with (
                patch("repopilot_guard.cli.AppSettings", return_value=settings),
                patch("repopilot_guard.cli.SqliteCheckpointStore", return_value=checkpoint),
                patch("repopilot_guard.cli.create_live_graph", return_value=object()),
                patch("repopilot_guard.cli.GraphRunner", return_value=runner),
                patch("sys.stdout", StringIO()),
            ):
                exit_code = main([
                    "task", "start", "--repo", str(repository), "--task", "分析订单权限",
                    "--thread-id", "thread-visible", "--state-db", str(state_path), "--output", str(root / "runs"),
                ])

            self.assertEqual(0, exit_code)
            self.assertEqual("RUNNING", observed["status"])
            self.assertIsNotNone(observed["lease"])

    def test_task_start_runtime_failure_is_sanitized_and_clears_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state_path = root / "state.sqlite"
            repository = root / "repository"
            repository.mkdir()
            _initialize_git_repository(repository)
            checkpoint = SimpleNamespace(checkpointer=object(), close=Mock())
            settings = SimpleNamespace(task_budget=lambda: TaskBudget())
            runner = Mock()
            runner.run.side_effect = RuntimeError("api_key=never-print-this")
            output = StringIO()
            with (
                patch("repopilot_guard.cli.AppSettings", return_value=settings),
                patch("repopilot_guard.cli.SqliteCheckpointStore", return_value=checkpoint),
                patch("repopilot_guard.cli.create_live_graph", return_value=object()),
                patch("repopilot_guard.cli.GraphRunner", return_value=runner),
                patch("sys.stdout", output),
            ):
                exit_code = main([
                    "task", "start", "--repo", str(repository), "--task", "分析订单权限",
                    "--thread-id", "thread-runtime-failure", "--state-db", str(state_path), "--output", str(root / "runs"),
                ])

            payload = json.loads(output.getvalue())
            self.assertEqual(2, exit_code)
            self.assertEqual("TASK_RUNTIME_FAILED", payload["code"])
            self.assertNotIn("never-print-this", output.getvalue())
            store = TaskStore(state_path)
            try:
                stored = store.get("thread-runtime-failure")
            finally:
                store.close()
            self.assertEqual("BLOCKED", stored.status)
            self.assertEqual("TASK_RUNTIME_FAILED: RuntimeError", stored.error_summary)
            self.assertIsNone(stored.lease_expires_at)

            status_output = StringIO()
            with (
                patch("repopilot_guard.cli.AppSettings") as settings,
                patch("repopilot_guard.cli.SqliteCheckpointStore") as checkpoint_store,
                patch("sys.stdout", status_output),
            ):
                status_exit_code = main(
                    [
                        "task",
                        "status",
                        "--thread-id",
                        "thread-runtime-failure",
                        "--state-db",
                        str(state_path),
                    ]
                )

            status_payload = json.loads(status_output.getvalue())
            self.assertEqual(2, status_exit_code)
            self.assertEqual("TASK_STATUS_INDEX_ONLY", status_payload["code"])
            self.assertEqual("BLOCKED", status_payload["status"])
            self.assertEqual("TASK_RUNTIME_FAILED: RuntimeError", status_payload["error_summary"])
            self.assertEqual("change", status_payload["task_operation"])
            settings.assert_not_called()
            checkpoint_store.assert_not_called()

    def test_task_start_rejects_duplicate_thread_id_without_running_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state_path = root / "state.sqlite"
            repository = root / "repository"
            repository.mkdir()
            _initialize_git_repository(repository)
            store = TaskStore(state_path)
            try:
                store.create(
                    thread_id="thread-duplicate", task_id="task-existing", project_id=None,
                    repository=repository, output_root=root / "existing-runs", task_mode="safe-isolated",
                    permission_mode="safe", workspace_mode="worktree",
                )
            finally:
                store.close()
            checkpoint = SimpleNamespace(checkpointer=object(), close=Mock())
            settings = SimpleNamespace(task_budget=lambda: TaskBudget())
            runner = Mock()
            output = StringIO()
            with (
                patch("repopilot_guard.cli.AppSettings", return_value=settings),
                patch("repopilot_guard.cli.SqliteCheckpointStore", return_value=checkpoint),
                patch("repopilot_guard.cli.create_live_graph", return_value=object()),
                patch("repopilot_guard.cli.GraphRunner", return_value=runner),
                patch("sys.stdout", output),
            ):
                exit_code = main([
                    "task", "start", "--repo", str(repository), "--task", "不得覆盖旧任务",
                    "--thread-id", "thread-duplicate", "--state-db", str(state_path), "--output", str(root / "runs"),
                ])

            self.assertEqual(2, exit_code)
            self.assertEqual("THREAD_ID_ALREADY_EXISTS", json.loads(output.getvalue())["code"])
            runner.run.assert_not_called()

    def test_non_git_full_local_change_is_blocked_before_runtime_loading(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "plain-project"
            repository.mkdir()
            state_path = root / "state.sqlite"
            output = StringIO()
            with (
                patch("repopilot_guard.cli.AppSettings") as settings,
                patch("repopilot_guard.cli.SqliteCheckpointStore") as checkpoint_store,
                patch("repopilot_guard.cli.GraphRunner") as runner,
                patch("sys.stdout", output),
            ):
                exit_code = main(
                    [
                        "task",
                        "start",
                        "--repo",
                        str(repository),
                        "--task",
                        "直接修改代码",
                        "--task-mode",
                        "full-local",
                        "--confirm-full-access",
                        FULL_ACCESS_CONFIRMATION,
                        "--state-db",
                        str(state_path),
                    ]
                )

            payload = json.loads(output.getvalue())
            self.assertEqual(2, exit_code)
            self.assertEqual("FULL_LOCAL_CHANGE_REQUIRES_GIT", payload["code"])
            self.assertEqual(["research"], payload["allowed_operations"])
            self.assertFalse(state_path.exists())
            settings.assert_not_called()
            checkpoint_store.assert_not_called()
            runner.assert_not_called()

    def test_dirty_safe_task_is_blocked_before_runtime_loading(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "dirty-project"
            repository.mkdir()
            _initialize_git_repository(repository)
            (repository / "README.md").write_text("# dirty fixture\n", encoding="utf-8")
            state_path = root / "state.sqlite"
            output = StringIO()
            with (
                patch("repopilot_guard.cli.AppSettings") as settings,
                patch("repopilot_guard.cli.SqliteCheckpointStore") as checkpoint_store,
                patch("repopilot_guard.cli.GraphRunner") as runner,
                patch("sys.stdout", output),
            ):
                exit_code = main(
                    [
                        "task",
                        "start",
                        "--repo",
                        str(repository),
                        "--task",
                        "分析未提交改动",
                        "--state-db",
                        str(state_path),
                    ]
                )

            payload = json.loads(output.getvalue())
            self.assertEqual(2, exit_code)
            self.assertEqual("DIRTY_SOURCE_BLOCKED", payload["code"])
            self.assertFalse(state_path.exists())
            settings.assert_not_called()
            checkpoint_store.assert_not_called()
            runner.assert_not_called()

    def test_cli_lease_heartbeat_renews_until_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = TaskStore(root / "state.sqlite")
            try:
                store.create(
                    thread_id="thread-heartbeat", task_id="task-heartbeat", project_id=None,
                    repository=root / "repo", output_root=root / "runs", task_mode="safe-isolated",
                    permission_mode="safe", workspace_mode="worktree",
                )
                store.begin_execution("thread-heartbeat")
                stop = Event()
                with patch.object(store, "renew_lease", wraps=store.renew_lease) as renew:
                    worker = _start_cli_lease_heartbeat(store, "thread-heartbeat", stop, interval_seconds=0.01)
                    time.sleep(0.04)
                    stop.set()
                    worker.join(timeout=1)
                self.assertGreaterEqual(renew.call_count, 1)
                self.assertFalse(worker.is_alive())
            finally:
                store.close()

    def test_full_local_without_confirmation_is_blocked_before_graph_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output = StringIO()
            with patch("sys.stdout", output):
                exit_code = main(
                    [
                        "task",
                        "start",
                        "--repo",
                        str(root),
                        "--task",
                        "直接修改代码",
                        "--task-mode",
                        "full-local",
                    ]
                )

            self.assertEqual(2, exit_code)
            payload = json.loads(output.getvalue())
            self.assertEqual("TASK_CONFIGURATION_OR_INPUT_INVALID", payload["code"])

    def test_document_add_returns_service_result_and_preserves_blocked_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state_path = root / "state.sqlite"
            repository = root / "repo"
            repository.mkdir()
            registry = ProjectRegistry(state_path)
            project = registry.add(repository, "文档项目")
            registry.close()
            output = StringIO()
            with (
                patch(
                    "repopilot_guard.cli.index_uploaded_document",
                    return_value={"status": "READY", "code": "CONTEXT_INDEXED", "document": {"display_name": "requirements.md"}},
                ) as indexer,
                patch("sys.stdout", output),
            ):
                exit_code = main(
                    ["document", "add", "--project-id", project.project_id, "--file", str(root / "requirements.md"), "--state-db", str(state_path)]
                )
            self.assertEqual(0, exit_code)
            self.assertEqual(project.project_id, indexer.call_args.args[1])
            self.assertEqual("CONTEXT_INDEXED", json.loads(output.getvalue())["code"])

            output = StringIO()
            with (
                patch("repopilot_guard.cli.index_uploaded_document", return_value={"status": "BLOCKED", "code": "DOCUMENT_UNREADABLE"}),
                patch("sys.stdout", output),
            ):
                exit_code = main(
                    ["document", "add", "--project-id", project.project_id, "--file", str(root / "blocked.txt"), "--state-db", str(state_path)]
                )
            self.assertEqual(2, exit_code)
            self.assertEqual("DOCUMENT_UNREADABLE", json.loads(output.getvalue())["code"])

    def test_document_list_hides_managed_and_original_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state_path = root / "state.sqlite"
            repository = root / "repo"
            repository.mkdir()
            original = root / "outside-requirements.md"
            original.write_text("# 订单需求\n", encoding="utf-8")
            registry = ProjectRegistry(state_path)
            project = registry.add(repository, "文档项目")
            registry.close()
            managed = ManagedDocumentStore(state_path).import_document(original, project_id=project.project_id)
            output = StringIO()
            with patch("sys.stdout", output):
                exit_code = main(["document", "list", "--project-id", project.project_id, "--state-db", str(state_path)])
            payload = json.loads(output.getvalue())
            encoded = json.dumps(payload, ensure_ascii=False)
            self.assertEqual(0, exit_code)
            self.assertEqual(managed.document_id, payload["documents"][0]["document_id"])
            self.assertNotIn(str(managed.managed_path), encoded)
            self.assertNotIn(str(original), encoded)

    def test_desktop_doctor_distinguishes_preview_from_missing_windows_linker(self) -> None:
        def tool_version(*candidates: str) -> str | None:
            return None if candidates == ("link.exe",) else "tool version"

        output = StringIO()
        with (
            patch("repopilot_guard.cli._desktop_command_version", side_effect=tool_version),
            patch(
                "repopilot_guard.cli._desktop_backend_delivery_check",
                return_value=ComponentCheck("backend_sidecar", True, "BACKEND_SIDECAR_READY", "sidecar ready"),
            ),
            patch("sys.stdout", output),
        ):
            exit_code = main(["desktop", "doctor"])
        payload = json.loads(output.getvalue())
        self.assertEqual(2, exit_code)
        self.assertEqual("READY", payload["preview"]["status"])
        self.assertEqual("BLOCKED", payload["package"]["status"])
        linker = next(item for item in payload["checks"] if item["component"] == "linker")
        self.assertEqual("LINKER_MISSING", linker["code"])

    def test_desktop_doctor_is_ready_when_all_tools_are_available(self) -> None:
        output = StringIO()
        with (
            patch("repopilot_guard.cli._desktop_command_version", return_value="tool version"),
            patch(
                "repopilot_guard.cli._desktop_backend_delivery_check",
                return_value=ComponentCheck("backend_sidecar", True, "BACKEND_SIDECAR_READY", "sidecar ready"),
            ),
            patch("sys.stdout", output),
        ):
            exit_code = main(["desktop", "doctor"])
        payload = json.loads(output.getvalue())
        self.assertEqual(0, exit_code)
        self.assertEqual("READY", payload["package"]["status"])

    def test_windows_build_tool_discovery_uses_standard_installation_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            program_files = Path(temporary_directory)
            linker = (
                program_files
                / "Microsoft Visual Studio"
                / "2022"
                / "BuildTools"
                / "VC"
                / "Tools"
                / "MSVC"
                / "14.44.35207"
                / "bin"
                / "Hostx64"
                / "x64"
                / "link.exe"
            )
            linker.parent.mkdir(parents=True)
            linker.write_bytes(b"test")
            with patch.dict(os.environ, {"ProgramFiles": str(program_files), "ProgramFiles(x86)": ""}):
                discovered = _windows_build_tool_candidates("link.exe")

        self.assertEqual((linker,), discovered)

    def test_windows_build_tools_are_not_called_with_unsupported_version_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            linker = Path(temporary_directory) / "link.exe"
            linker.write_bytes(b"test")
            with (
                patch("repopilot_guard.cli._desktop_executable_candidates", return_value=(str(linker),)),
                patch("repopilot_guard.cli.subprocess.run") as run,
            ):
                version = _desktop_command_version("link.exe")

        self.assertEqual(str(linker), version)
        run.assert_not_called()

    def test_desktop_sidecar_requires_declared_resource_and_binary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            desktop_root = Path(temporary_directory)
            tauri_root = desktop_root / "src-tauri"
            binary = tauri_root / "binaries" / "repopilot-guard.exe"
            binary.parent.mkdir(parents=True)
            config_path = tauri_root / "tauri.conf.json"
            config_path.write_text(
                json.dumps({"bundle": {"resources": ["binaries/repopilot-guard.exe"]}}),
                encoding="utf-8",
            )

            missing = _desktop_backend_delivery_check(desktop_root, config_path)
            self.assertFalse(missing.ready)
            self.assertEqual("BACKEND_SIDECAR_MISSING", missing.code)

            binary.write_bytes(b"sidecar")
            ready = _desktop_backend_delivery_check(desktop_root, config_path)
            self.assertTrue(ready.ready)
            self.assertEqual("BACKEND_SIDECAR_READY", ready.code)

    def test_desktop_paths_are_stable_and_do_not_create_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            app_data = Path(temporary_directory) / "AppData" / "Roaming"
            output = StringIO()
            with (
                patch.dict(os.environ, {"APPDATA": str(app_data)}, clear=True),
                patch("sys.stdout", output),
            ):
                exit_code = main(["desktop", "paths"])

            payload = json.loads(output.getvalue())
            expected = app_data / "com.repopilot.desktop"
            self.assertEqual(0, exit_code)
            self.assertEqual(str(expected), payload["runtime_dir"])
            self.assertEqual(str(expected / "settings.env"), payload["config_file"])
            self.assertEqual(str(expected / "state.sqlite"), payload["state_db"])
            self.assertFalse(expected.exists())

    def test_desktop_paths_honor_absolute_runtime_directory_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            runtime_dir = Path(temporary_directory) / "repopilot-runtime"
            output = StringIO()
            with (
                patch.dict(os.environ, {"REPOPILOT_DESKTOP_DATA_DIR": str(runtime_dir)}, clear=True),
                patch("sys.stdout", output),
            ):
                exit_code = main(["desktop", "paths"])

            payload = json.loads(output.getvalue())
            self.assertEqual(0, exit_code)
            self.assertEqual(str(runtime_dir.resolve()), payload["runtime_dir"])
            self.assertEqual(str(runtime_dir.resolve() / "settings.env"), payload["config_file"])
            self.assertEqual(str(runtime_dir.resolve() / "state.sqlite"), payload["state_db"])
            self.assertFalse(runtime_dir.exists())

    def test_desktop_init_config_creates_template_once_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            runtime_dir = Path(temporary_directory) / "repopilot-runtime"
            output = StringIO()
            with (
                patch.dict(os.environ, {"REPOPILOT_DESKTOP_DATA_DIR": str(runtime_dir)}, clear=True),
                patch("sys.stdout", output),
            ):
                exit_code = main(["desktop", "init-config"])

            payload = json.loads(output.getvalue())
            config_file = runtime_dir / "settings.env"
            self.assertEqual(0, exit_code)
            self.assertEqual("DESKTOP_CONFIG_TEMPLATE_CREATED", payload["code"])
            self.assertEqual(str(config_file.resolve()), payload["config_file"])
            self.assertIn("REPOPILOT_CHAT_API_KEY=", config_file.read_text(encoding="utf-8"))

            original = config_file.read_text(encoding="utf-8")
            output = StringIO()
            with (
                patch.dict(os.environ, {"REPOPILOT_DESKTOP_DATA_DIR": str(runtime_dir)}, clear=True),
                patch("sys.stdout", output),
            ):
                repeated_exit_code = main(["desktop", "init-config"])

            repeated = json.loads(output.getvalue())
            self.assertEqual(2, repeated_exit_code)
            self.assertEqual("DESKTOP_CONFIG_FILE_EXISTS", repeated["code"])
            self.assertEqual(original, config_file.read_text(encoding="utf-8"))

    def test_task_artifact_commands_list_and_verify_persisted_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state_path = root / "state.sqlite"
            store = TaskStore(state_path)
            try:
                store.create(
                    thread_id="thread-artifact-cli",
                    task_id="task-artifact-cli",
                    project_id="project-1",
                    repository=root / "repo",
                    output_root=root / "runs",
                    task_mode="safe-isolated",
                    permission_mode="safe",
                    workspace_mode="worktree",
                )
            finally:
                store.close()

            pending_archive = root / "exports" / "pending.zip"
            output = StringIO()
            with patch("sys.stdout", output):
                exit_code = main(
                    [
                        "task",
                        "export",
                        "--thread-id",
                        "thread-artifact-cli",
                        "--output",
                        str(pending_archive),
                        "--state-db",
                        str(state_path),
                    ]
                )
            self.assertEqual(2, exit_code)
            self.assertEqual("TASK_EXPORT_NOT_FINALIZED", json.loads(output.getvalue())["code"])
            self.assertFalse(pending_archive.exists())
            self.assertFalse(pending_archive.parent.exists())

            store = TaskStore(state_path)
            try:
                store.sync_graph_result(
                    {
                        "thread_id": "thread-artifact-cli",
                        "status": "REPORT",
                        "pending_approval": False,
                        "verdict": "UNVERIFIED",
                        "state": {
                            "tool_events": [],
                            "plan": {"summary": "检查订单租户过滤", "candidate_files": ["src/OrderService.java"], "steps": ["增加过滤"]},
                            "verification_result": {"status": "UNVERIFIED"},
                            "git_diff": "",
                        },
                    }
                )
                plan_artifact = next(item for item in store.artifacts("thread-artifact-cli") if item.kind == "plan_json")
            finally:
                store.close()

            output = StringIO()
            with patch("sys.stdout", output):
                exit_code = main(["task", "artifacts", "--thread-id", "thread-artifact-cli", "--state-db", str(state_path)])
            listed = json.loads(output.getvalue())
            self.assertEqual(0, exit_code)
            self.assertIn("plan_json", {item["kind"] for item in listed["artifacts"]})

            output = StringIO()
            with patch("sys.stdout", output):
                exit_code = main(["task", "artifact", "--thread-id", "thread-artifact-cli", "--kind", "plan_json", "--state-db", str(state_path)])
            artifact_payload = json.loads(output.getvalue())
            self.assertEqual(0, exit_code)
            self.assertIn("检查订单租户过滤", artifact_payload["content"])

            artifact_path = root / "runs" / "task-artifact-cli" / plan_artifact.relative_path
            artifact_path.write_text("tampered", encoding="utf-8")
            output = StringIO()
            with patch("sys.stdout", output):
                exit_code = main(["task", "artifact", "--thread-id", "thread-artifact-cli", "--kind", "plan_json", "--state-db", str(state_path)])
            self.assertEqual(2, exit_code)
            self.assertEqual("TASK_ARTIFACT_INTEGRITY_MISMATCH", json.loads(output.getvalue())["code"])

    def test_task_export_creates_portable_audit_bundle_and_rejects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state_path = root / "state.sqlite"
            repository = root / "private-repository"
            output_root = root / "private-runs"
            archive_path = root / "exports" / "audit.zip"
            store = TaskStore(state_path)
            try:
                store.create(
                    thread_id="thread-export-cli",
                    task_id="task-export-cli",
                    project_id="project-1",
                    repository=repository,
                    output_root=output_root,
                    task_mode="safe-isolated",
                    permission_mode="safe",
                    workspace_mode="worktree",
                )
                store.sync_graph_result(
                    {
                        "thread_id": "thread-export-cli",
                        "status": "REPORT",
                        "pending_approval": False,
                        "verdict": "PASSED",
                        "state": {
                            "tool_events": [{"type": "MODEL_USAGE", "api_key": "never-export-this"}],
                            "plan": {"summary": "修复订单校验", "candidate_files": ["src/OrderController.java"], "steps": ["增加校验"]},
                            "git_diff": "diff --git a/src/OrderController.java b/src/OrderController.java\n",
                            "verification_result": {"status": "PASSED", "code": "MAVEN_SUCCEEDED"},
                        },
                    }
                )
                report = next(item for item in store.artifacts("thread-export-cli") if item.kind == "report")
            finally:
                store.close()

            output = StringIO()
            with patch("sys.stdout", output):
                exit_code = main(
                    [
                        "task",
                        "export",
                        "--thread-id",
                        "thread-export-cli",
                        "--output",
                        str(archive_path),
                        "--state-db",
                        str(state_path),
                    ]
                )
            payload = json.loads(output.getvalue())
            self.assertEqual(0, exit_code)
            self.assertEqual("TASK_EVIDENCE_EXPORTED", payload["code"])
            self.assertTrue(archive_path.is_file())
            self.assertEqual(64, len(payload["export"]["sha256"]))

            with zipfile.ZipFile(archive_path) as archive:
                names = set(archive.namelist())
                manifest = json.loads(archive.read("manifest.json"))
                evidence = archive.read("evidence.jsonl").decode("utf-8")
            self.assertIn("artifacts/report.md", names)
            self.assertIn("artifacts/changes.diff", names)
            self.assertIn("evidence.jsonl", names)
            self.assertEqual("thread-export-cli", manifest["task"]["thread_id"])
            self.assertNotIn(str(repository), json.dumps(manifest, ensure_ascii=False))
            self.assertNotIn(str(output_root), json.dumps(manifest, ensure_ascii=False))
            self.assertNotIn("never-export-this", evidence)
            self.assertIn("[REDACTED]", evidence)

            output = StringIO()
            with patch("sys.stdout", output):
                exit_code = main(
                    [
                        "task",
                        "export",
                        "--thread-id",
                        "thread-export-cli",
                        "--output",
                        str(archive_path),
                        "--state-db",
                        str(state_path),
                    ]
                )
            self.assertEqual(2, exit_code)
            self.assertEqual("TASK_EXPORT_OUTPUT_EXISTS", json.loads(output.getvalue())["code"])

            artifact_path = output_root / "task-export-cli" / report.relative_path
            artifact_path.write_text("tampered", encoding="utf-8")
            tampered_archive = root / "exports" / "tampered.zip"
            output = StringIO()
            with patch("sys.stdout", output):
                exit_code = main(
                    [
                        "task",
                        "export",
                        "--thread-id",
                        "thread-export-cli",
                        "--output",
                        str(tampered_archive),
                        "--state-db",
                        str(state_path),
                    ]
                )
            self.assertEqual(2, exit_code)
            self.assertEqual("TASK_ARTIFACT_INTEGRITY_MISMATCH", json.loads(output.getvalue())["code"])
            self.assertFalse(tampered_archive.exists())

    def test_task_management_commands_list_events_and_archive_without_path_leakage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state_path = root / "state.sqlite"
            repository = root / "private-repository"
            output_root = root / "private-runs"
            store = TaskStore(state_path)
            try:
                store.create(
                    thread_id="thread-management-cli",
                    task_id="task-management-cli",
                    project_id="project-1",
                    repository=repository,
                    output_root=output_root,
                    task_mode="safe-isolated",
                    task_operation="research",
                    permission_mode="safe",
                    workspace_mode="worktree",
                )
                store.sync_graph_result(
                    {
                        "thread_id": "thread-management-cli",
                        "status": "BLOCKED",
                        "pending_approval": False,
                        "verdict": "BLOCKED",
                        "state": {
                            "tool_events": [{"type": "MODEL_BLOCKED", "api_key": "never-print-this"}],
                            "error_summary": "EXPECTED_BLOCK",
                        },
                    }
                )
            finally:
                store.close()

            output = StringIO()
            with patch("sys.stdout", output):
                exit_code = main(["task", "list", "--state-db", str(state_path)])
            listed = json.loads(output.getvalue())
            encoded_list = json.dumps(listed, ensure_ascii=False)
            self.assertEqual(0, exit_code)
            self.assertEqual("TASK_LIST_READY", listed["code"])
            self.assertEqual(1, listed["count"])
            self.assertEqual("research", listed["tasks"][0]["task_operation"])
            self.assertNotIn(str(repository), encoded_list)
            self.assertNotIn(str(output_root), encoded_list)

            output = StringIO()
            with patch("sys.stdout", output):
                exit_code = main(
                    [
                        "task",
                        "events",
                        "--thread-id",
                        "thread-management-cli",
                        "--limit",
                        "10",
                        "--state-db",
                        str(state_path),
                    ]
                )
            events = json.loads(output.getvalue())
            encoded_events = json.dumps(events, ensure_ascii=False)
            self.assertEqual(0, exit_code)
            self.assertEqual("TASK_EVENTS_READY", events["code"])
            self.assertGreater(events["next_sequence"], 0)
            self.assertIn("[REDACTED]", encoded_events)
            self.assertNotIn("never-print-this", encoded_events)

            output = StringIO()
            with patch("sys.stdout", output):
                exit_code = main(
                    ["task", "archive", "--thread-id", "thread-management-cli", "--state-db", str(state_path)]
                )
            archived = json.loads(output.getvalue())
            self.assertEqual(0, exit_code)
            self.assertEqual("TASK_ARCHIVED", archived["code"])
            self.assertIsNotNone(archived["task"]["archived_at"])

            output = StringIO()
            with patch("sys.stdout", output):
                main(["task", "list", "--state-db", str(state_path)])
            self.assertEqual(0, json.loads(output.getvalue())["count"])

            output = StringIO()
            with patch("sys.stdout", output):
                main(["task", "list", "--include-archived", "--state-db", str(state_path)])
            self.assertEqual(1, json.loads(output.getvalue())["count"])

    def test_task_watch_streams_sanitized_events_and_finishes_for_terminal_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state_path = root / "state.sqlite"
            repository = root / "private-repository"
            output_root = root / "private-runs"
            store = TaskStore(state_path)
            try:
                store.create(
                    thread_id="thread-watch-terminal",
                    task_id="task-watch-terminal",
                    project_id="project-1",
                    repository=repository,
                    output_root=output_root,
                    task_mode="safe-isolated",
                    permission_mode="safe",
                    workspace_mode="worktree",
                )
                store.sync_graph_result(
                    {
                        "thread_id": "thread-watch-terminal",
                        "status": "REPORT",
                        "pending_approval": False,
                        "verdict": "PASSED",
                        "state": {
                            "tool_events": [
                                {"type": "PLAN_GENERATED", "token": "never-print-this"}
                            ],
                        },
                    }
                )
            finally:
                store.close()

            output = StringIO()
            with patch("sys.stdout", output):
                exit_code = main(
                    [
                        "task",
                        "watch",
                        "--thread-id",
                        "thread-watch-terminal",
                        "--timeout-seconds",
                        "0",
                        "--state-db",
                        str(state_path),
                    ]
                )

            lines = [json.loads(line) for line in output.getvalue().splitlines()]
            encoded = json.dumps(lines, ensure_ascii=False)
            self.assertEqual(0, exit_code)
            self.assertEqual("TASK_WATCH_STARTED", lines[0]["code"])
            self.assertIn("TASK_WATCH_EVENT", [line["code"] for line in lines])
            self.assertEqual("TASK_WATCH_FINISHED", lines[-1]["code"])
            self.assertEqual("PASSED", lines[-1]["task"]["verdict"])
            self.assertNotIn(str(repository), encoded)
            self.assertNotIn(str(output_root), encoded)
            self.assertNotIn("never-print-this", encoded)

    def test_task_watch_timeout_reports_a_resumable_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state_path = root / "state.sqlite"
            store = TaskStore(state_path)
            try:
                store.create(
                    thread_id="thread-watch-running",
                    task_id="task-watch-running",
                    project_id="project-1",
                    repository=root / "repo",
                    output_root=root / "runs",
                    task_mode="safe-isolated",
                    permission_mode="safe",
                    workspace_mode="worktree",
                )
            finally:
                store.close()

            output = StringIO()
            with patch("sys.stdout", output):
                exit_code = main(
                    [
                        "task",
                        "watch",
                        "--thread-id",
                        "thread-watch-running",
                        "--timeout-seconds",
                        "0",
                        "--state-db",
                        str(state_path),
                    ]
                )

            lines = [json.loads(line) for line in output.getvalue().splitlines()]
            timeout = lines[-1]
            self.assertEqual(0, exit_code)
            self.assertEqual("TASK_WATCH_TIMEOUT", timeout["code"])
            self.assertGreater(timeout["next_sequence"], 0)
            self.assertIn("--after-sequence", timeout["next_action"]["command"])
            self.assertIn(str(timeout["next_sequence"]), timeout["next_action"]["command"])

    def test_task_list_only_loads_local_state_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state_path = root / "state.sqlite"
            store = TaskStore(state_path)
            try:
                store.create(
                    thread_id="thread-config-isolation",
                    task_id="task-config-isolation",
                    project_id="project-1",
                    repository=root / "repo",
                    output_root=root / "runs",
                    task_mode="safe-isolated",
                    permission_mode="safe",
                    workspace_mode="worktree",
                )
            finally:
                store.close()

            output = StringIO()
            with (
                patch.dict(
                    os.environ,
                    {
                        "REPOPILOT_STATE_DB_PATH": str(state_path),
                        "REPOPILOT_EMBEDDING_DIMENSIONS": "invalid",
                    },
                    clear=False,
                ),
                patch("sys.stdout", output),
            ):
                exit_code = main(["task", "list"])

            payload = json.loads(output.getvalue())
            self.assertEqual(0, exit_code)
            self.assertEqual("TASK_LIST_READY", payload["code"])
            self.assertEqual("thread-config-isolation", payload["tasks"][0]["thread_id"])

    def test_task_status_falls_back_to_index_when_model_configuration_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state_path = root / "state.sqlite"
            store = TaskStore(state_path)
            try:
                store.create(
                    thread_id="thread-status-fallback",
                    task_id="task-status-fallback",
                    display_title="检查权限 API_KEY=do-not-print",
                    project_id="project-1",
                    repository=root / "private-repository",
                    output_root=root / "private-runs",
                    task_mode="safe-isolated",
                    permission_mode="safe",
                    workspace_mode="worktree",
                )
                store.sync_graph_result(
                    {
                        "thread_id": "thread-status-fallback",
                        "status": "WAITING_APPROVAL",
                        "pending_approval": True,
                        "verdict": None,
                        "state": {"tool_events": [], "error_summary": None},
                    }
                )
            finally:
                store.close()

            output = StringIO()
            with (
                patch.dict(
                    os.environ,
                    {
                        "REPOPILOT_STATE_DB_PATH": str(state_path),
                        "REPOPILOT_EMBEDDING_DIMENSIONS": "invalid",
                    },
                    clear=False,
                ),
                patch("sys.stdout", output),
            ):
                exit_code = main(
                    ["task", "status", "--thread-id", "thread-status-fallback"]
                )

            payload = json.loads(output.getvalue())
            encoded = json.dumps(payload, ensure_ascii=False)
            self.assertEqual(0, exit_code)
            self.assertEqual("TASK_STATUS_INDEX_ONLY", payload["code"])
            self.assertEqual("task_index", payload["source"])
            self.assertFalse(payload["detail_available"])
            self.assertTrue(payload["pending_approval"])
            self.assertIsNone(payload["plan"])
            self.assertIsNone(payload["verification"])
            self.assertIn("[REDACTED]", payload["display_title"])
            self.assertNotIn("do-not-print", encoded)
            self.assertNotIn(str(root / "private-repository"), encoded)
            self.assertNotIn(str(root / "private-runs"), encoded)


if __name__ == "__main__":
    unittest.main()
