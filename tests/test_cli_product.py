from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
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
            "interrupts": [{"type": "PLAN_APPROVAL_REQUIRED", "message": "请审阅计划。"}] if pending_approval else [],
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
                },
                "verification_result": None,
            },
        }

    def to_dict(self) -> dict[str, object]:
        return self._payload


class CliProductTests(unittest.TestCase):
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

        rust_source = (repository_root / "desktop" / "src-tauri" / "src" / "main.rs").read_text(encoding="utf-8")
        self.assertIn("app_data_dir", rust_source)
        self.assertIn("REPOPILOT_STATE_DB_PATH", rust_source)
        self.assertIn("CREATE_NO_WINDOW", rust_source)
        self.assertIn("WindowEvent::CloseRequested", rust_source)
        self.assertIn("app_handle.exit(0)", rust_source)

    def test_task_summary_excludes_raw_messages_and_tool_arguments(self) -> None:
        encoded = json.dumps(_task_summary(_Result()), ensure_ascii=False)

        self.assertIn("PLAN_GENERATED", encoded)
        self.assertIn("OrderService.java", encoded)
        self.assertNotIn("敏感任务正文不得出现在摘要中", encoded)
        self.assertNotIn("不得输出", encoded)

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
            self.assertEqual(str(expected / ".env"), payload["config_file"])
            self.assertEqual(str(expected / "state.sqlite"), payload["state_db"])
            self.assertFalse(expected.exists())

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
