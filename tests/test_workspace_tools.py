from __future__ import annotations

import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from repopilot_guard.cli import main
from repopilot_guard.evidence import EvidenceStore
from repopilot_guard.models import TaskRequest, WorkspaceMode, WorkspaceSelection
from repopilot_guard.permissions import FULL_ACCESS_CONFIRMATION, PermissionGrant, PermissionMode
from repopilot_guard.policy import PolicyGuard, ToolName
from repopilot_guard.repository_tools import RepositoryTools
from repopilot_guard.workspace import WorkspaceManager


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def create_java_repository(root: Path) -> Path:
    repository = root / "sample-java-repository"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "RepoPilot Test")
    _git(repository, "config", "user.email", "test@example.invalid")
    (repository / "src" / "main" / "java" / "com" / "example").mkdir(parents=True)
    (repository / "src" / "main" / "java" / "com" / "example" / "OrderService.java").write_text(
        "package com.example;\npublic class OrderService { }\n",
        encoding="utf-8",
    )
    (repository / "pom.xml").write_text(
        """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.example</groupId>
  <artifactId>sample-service</artifactId>
  <version>1.0.0</version>
</project>
""",
        encoding="utf-8",
    )
    (repository / "README.md").write_text("OrderService 负责订单查询。\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "initial fixture")
    return repository


class WorkspaceManagerTests(unittest.TestCase):
    def test_safe_mode_creates_detached_worktree_and_keeps_source_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = create_java_repository(root)
            request = TaskRequest(
                repository=repository,
                description="准备隔离仓库",
                output_root=root / "runs",
                task_id="task-clean",
            )
            manager = WorkspaceManager()
            before = manager.snapshot(repository)

            result = manager.prepare(request, PermissionGrant.safe())
            after = manager.snapshot(repository)

            self.assertEqual("READY", result.status)
            self.assertTrue(result.workspace_path and result.workspace_path.is_dir())
            self.assertEqual(before, after)
            self.assertTrue(result.source_unchanged)
            self.assertEqual(before.head_commit, _git(result.workspace_path, "rev-parse", "HEAD"))
            self.assertEqual("HEAD", _git(result.workspace_path, "rev-parse", "--abbrev-ref", "HEAD"))

    def test_safe_mode_blocks_dirty_source_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = create_java_repository(root)
            (repository / "README.md").write_text("dirty\n", encoding="utf-8")
            request = TaskRequest(repository, "准备隔离仓库", root / "runs", task_id="task-dirty-safe")

            result = WorkspaceManager().prepare(request, PermissionGrant.safe())

            self.assertEqual("BLOCKED", result.status)
            self.assertTrue(result.snapshot.is_dirty)
            self.assertIsNone(result.workspace_path)
            self.assertFalse((root / "runs" / "task-dirty-safe" / "worktree").exists())

    def test_explicit_migration_copies_dirty_source_without_modifying_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = create_java_repository(root)
            (repository / "README.md").write_text("dirty\n", encoding="utf-8")
            request = TaskRequest(
                repository,
                "准备隔离仓库",
                root / "runs",
                task_id="task-dirty-full",
                workspace_selection=WorkspaceSelection(include_uncommitted_changes=True),
            )
            permission = PermissionGrant(PermissionMode.FULL, FULL_ACCESS_CONFIRMATION)
            manager = WorkspaceManager()
            before = manager.snapshot(repository)

            result = manager.prepare(request, permission)
            after = manager.snapshot(repository)

            self.assertEqual("READY", result.status)
            self.assertTrue(result.snapshot.is_dirty)
            self.assertIn("迁移", result.message)
            self.assertEqual(before, after)
            self.assertEqual("dirty\n", (result.workspace_path / "README.md").read_text(encoding="utf-8"))
            self.assertEqual("USER_GRANTED_FULL_ACCESS", result.permission.to_dict()["audit_code"])

    def test_full_mode_requires_exact_confirmation(self) -> None:
        with self.assertRaisesRegex(ValueError, "FULL_ACCESS_CONFIRMATION_REQUIRED"):
            PermissionGrant(PermissionMode.FULL, "我确认")

    def test_repository_without_commit_is_blocked_with_baseline_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "empty-history-repository"
            repository.mkdir()
            _git(repository, "init", "-b", "main")
            (repository / "pom.xml").write_text("<project />", encoding="utf-8")
            request = TaskRequest(repository, "准备隔离仓库", root / "runs", task_id="task-no-commit")

            result = WorkspaceManager().prepare(request, PermissionGrant.safe())

        self.assertEqual("BLOCKED", result.status)
        self.assertEqual("GIT_BASELINE_UNAVAILABLE", result.code)
        self.assertIsNone(result.snapshot)

    def test_full_local_mode_can_research_non_git_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "plain-maven-project"
            repository.mkdir()
            (repository / "pom.xml").write_text("<project />\n", encoding="utf-8")
            request = TaskRequest(
                repository,
                "读取非 Git 项目",
                root / "runs",
                workspace_selection=WorkspaceSelection(mode=WorkspaceMode.LOCAL),
            )
            permission = PermissionGrant(PermissionMode.FULL, FULL_ACCESS_CONFIRMATION)

            result = WorkspaceManager().prepare(request, permission)

        self.assertEqual("READY", result.status)
        self.assertEqual("LOCAL_NON_GIT_WORKSPACE_READY", result.code)
        self.assertIsNone(result.snapshot)
        self.assertTrue(result.base_commit and result.base_commit.startswith("non-git-"))


class PermissionAndRepositoryToolsTests(unittest.TestCase):
    def test_safe_mode_blocks_sensitive_and_outside_paths_but_full_mode_records_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "workspace"
            workspace.mkdir()
            secret_file = workspace / ".env"
            secret_file.write_text("PASSWORD=secret", encoding="utf-8")
            outside = root / "outside.txt"
            outside.write_text("outside", encoding="utf-8")

            safe_guard = PolicyGuard(workspace, PermissionGrant.safe())
            full_guard = PolicyGuard(workspace, PermissionGrant(PermissionMode.FULL, FULL_ACCESS_CONFIRMATION))

            self.assertFalse(safe_guard.check_path(ToolName.READ_FILE, secret_file).allowed)
            self.assertFalse(safe_guard.check_path(ToolName.READ_FILE, outside).allowed)
            full = full_guard.check_path(ToolName.READ_FILE, outside)
            self.assertTrue(full.allowed)
            self.assertEqual("USER_GRANTED_FULL_ACCESS", full.audit_code)

    def test_read_only_tools_respect_limits_and_write_audit_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = create_java_repository(root)
            evidence = EvidenceStore(root / "runs", "tool-events")
            tools = RepositoryTools(repository, PermissionGrant.safe(), evidence)
            deep_file = repository / "a" / "b" / "c" / "d" / "e" / "f" / "g" / "deep.txt"
            deep_file.parent.mkdir(parents=True)
            deep_file.write_text("too deep", encoding="utf-8")
            (repository / ".env").write_text("TOKEN=secret", encoding="utf-8")
            (repository / "binary.bin").write_bytes(b"\0\x01")
            (repository / "large.txt").write_bytes(b"x" * (256 * 1024 + 1))

            listed = tools.list_files(max_depth=1)
            searched = tools.search_code("OrderService")
            read = tools.read_file(Path("README.md"))
            secret = tools.read_file(Path(".env"))
            binary = tools.read_file(Path("binary.bin"))
            large = tools.read_file(Path("large.txt"))
            build = tools.inspect_build()

            self.assertEqual("READY", listed.status)
            self.assertNotIn(".env", listed.data["files"])
            self.assertNotIn("a/b/c/d/e/f/g/deep.txt", listed.data["files"])
            self.assertEqual("READY", searched.status)
            self.assertIn(
                "src/main/java/com/example/OrderService.java",
                {item["path"] for item in searched.data["matches"]},
            )
            self.assertEqual("READY", read.status)
            self.assertEqual("BLOCKED", secret.status)
            self.assertEqual("BINARY_FILE", binary.code)
            self.assertEqual("FILE_TOO_LARGE", large.code)
            self.assertEqual("sample-service", build.data["pom"]["artifact_id"])
            event_types = [line for line in evidence.events_path.read_text(encoding="utf-8").splitlines()]
            self.assertGreaterEqual(len(event_types), 7)


class WorkspaceCliTests(unittest.TestCase):
    def test_full_mode_requires_confirmation_in_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = create_java_repository(root)
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    [
                        "workspace",
                        "prepare",
                        "--repo",
                        str(repository),
                        "--task",
                        "准备隔离仓库",
                        "--permission",
                        "full",
                    ]
                )

        self.assertEqual(2, exit_code)
        self.assertIn("FULL_ACCESS_CONFIRMATION_REQUIRED", output.getvalue())

    def test_safe_mode_cli_returns_workspace_snapshot_and_audit_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = create_java_repository(root)
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    [
                        "workspace",
                        "prepare",
                        "--repo",
                        str(repository),
                        "--task",
                        "准备隔离仓库",
                        "--output",
                        str(root / "runs"),
                    ]
                )

        self.assertEqual(0, exit_code)
        self.assertIn('"status": "READY"', output.getvalue())
        self.assertIn('"evidence_events_path"', output.getvalue())
