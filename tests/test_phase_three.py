from __future__ import annotations

import subprocess
import tempfile
import unittest
import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import call, patch

import httpx
from openai import APIConnectionError

from repopilot_guard.cli import main
from repopilot_guard.context import (
    ContextChunkStore,
    ContextIndexer,
    ContextLoader,
    ContextRetriever,
    ManagedDocumentStore,
    ProjectMemoryRetriever,
    RetrievedContext,
    VerifiedProjectMemoryWriter,
    _hybrid_rerank,
    _qdrant_point_id,
    _with_transient_retry,
    split_context,
)
from repopilot_guard.models import TaskRequest, WorkspaceMode, WorkspaceSelection
from repopilot_guard.permissions import PermissionGrant
from repopilot_guard.project_registry import ProjectRegistry
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


def create_repository(root: Path) -> Path:
    repository = root / "project"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "RepoPilot Test")
    _git(repository, "config", "user.email", "test@example.invalid")
    source = repository / "src" / "main" / "java" / "com" / "example"
    source.mkdir(parents=True)
    (source / "OrderService.java").write_text("package com.example;\nclass OrderService { }\n", encoding="utf-8")
    (repository / "pom.xml").write_text("<project><artifactId>demo</artifactId></project>\n", encoding="utf-8")
    (repository / "README.md").write_text("# 订单\nOrderService 负责订单查询。\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "initial")
    return repository


class FakeEmbeddings:
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(text)), 1.0] for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return [float(len(text)), 1.0]


class FailingEmbeddings(FakeEmbeddings):
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedding unavailable")


class FakePoint:
    def __init__(self, payload: dict[str, object], score: float = 0.9) -> None:
        self.payload = payload
        self.score = score


class FakeQueryResponse:
    def __init__(self, points: list[FakePoint]) -> None:
        self.points = points


class FakeQdrant:
    def __init__(self) -> None:
        self.points: dict[str, object] = {}
        self.upsert_calls = 0

    def upsert(self, *, collection_name: str, points: list[object], wait: bool) -> None:
        self.upsert_calls += 1
        for point in points:
            self.points[str(point.id)] = point

    def query_points(self, *, collection_name: str, query: list[float], query_filter: object, limit: int, **_: object) -> FakeQueryResponse:
        expected = {condition.key: condition.match.value for condition in query_filter.must}
        matches = [
            FakePoint(point.payload)
            for point in self.points.values()
            if all(point.payload.get(key) == value for key, value in expected.items())
        ]
        return FakeQueryResponse(matches[:limit])


class RankedFakeQdrant:
    """按模拟向量分数返回候选，供混合重排断言使用。"""

    def __init__(self, points: list[FakePoint]) -> None:
        self._points = points
        self.last_limit = 0

    def query_points(self, *, limit: int, **_: object) -> FakeQueryResponse:
        self.last_limit = limit
        return FakeQueryResponse(self._points[:limit])


class MemoryFakeQdrant:
    """按 Collection 保存 point，模拟 project_memory 的项目级过滤。"""

    def __init__(self) -> None:
        self.points: dict[str, dict[str, object]] = {}

    def upsert(self, *, collection_name: str, points: list[object], wait: bool) -> None:
        collection = self.points.setdefault(collection_name, {})
        for point in points:
            collection[str(point.id)] = point

    def query_points(self, *, collection_name: str, query_filter: object, limit: int, **_: object) -> FakeQueryResponse:
        expected = {condition.key: condition.match.value for condition in query_filter.must}
        matches = [
            FakePoint(point.payload)
            for point in self.points.get(collection_name, {}).values()
            if all(point.payload.get(key) == value for key, value in expected.items())
        ]
        return FakeQueryResponse(matches[:limit])


class ProjectRegistryTests(unittest.TestCase):
    def test_add_is_idempotent_and_persists_after_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = create_repository(root)
            database = root / "state.sqlite"
            registry = ProjectRegistry(database)
            first = registry.add(repository / ".")
            second = registry.add(repository)
            self.assertEqual(first.project_id, second.project_id)
            registry.close()

            reopened = ProjectRegistry(database)
            self.assertEqual(repository.resolve(), reopened.get(first.project_id).root_path)
            self.assertTrue(reopened.remove(first.project_id))
            self.assertFalse(reopened.remove(first.project_id))
            reopened.close()

    def test_project_cli_uses_state_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = create_repository(root)
            output = StringIO()
            with redirect_stdout(output):
                code = main(["project", "add", "--path", str(repository), "--state-db", str(root / "state.sqlite")])
        self.assertEqual(0, code)
        self.assertIn('"project_id"', output.getvalue())

    def test_registered_project_refreshes_git_state_after_user_initializes_git(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project_root = root / "plain-project"
            project_root.mkdir()
            registry = ProjectRegistry(root / "state.sqlite")
            project = registry.add(project_root)
            self.assertFalse(project.is_git_repository)

            (project_root / ".git").mkdir()
            self.assertTrue(registry.get(project.project_id).is_git_repository)
            self.assertTrue(registry.list()[0].is_git_repository)
            registry.close()

    def test_project_doctor_reports_mode_specific_readiness_without_creating_a_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = create_repository(root)
            state_path = root / "state.sqlite"
            registry = ProjectRegistry(state_path)
            project = registry.add(repository)
            registry.close()
            output = StringIO()
            with redirect_stdout(output):
                code = main(["project", "doctor", "--project-id", project.project_id, "--state-db", str(state_path)])

        payload = json.loads(output.getvalue())
        self.assertEqual(0, code)
        self.assertEqual("safe-isolated", payload["recommended_task_mode"])
        self.assertEqual("READY", payload["task_modes"]["safe_isolated"]["status"])
        self.assertEqual("READY", payload["task_modes"]["full_local"]["status"])
        self.assertIsNotNone(payload["git"]["baseline_commit"])

    def test_project_doctor_marks_non_git_project_as_full_local_research_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project_root = root / "plain-project"
            project_root.mkdir()
            state_path = root / "state.sqlite"
            registry = ProjectRegistry(state_path)
            project = registry.add(project_root)
            registry.close()
            output = StringIO()
            with redirect_stdout(output):
                code = main(["project", "doctor", "--project-id", project.project_id, "--state-db", str(state_path)])

        payload = json.loads(output.getvalue())
        self.assertEqual(0, code)
        self.assertEqual("full-local", payload["recommended_task_mode"])
        self.assertEqual("GIT_REPOSITORY_REQUIRED", payload["task_modes"]["safe_isolated"]["code"])
        self.assertEqual("FULL_LOCAL_RESEARCH_ONLY", payload["task_modes"]["full_local"]["code"])


class WorkspaceSelectionTests(unittest.TestCase):
    def test_local_selection_does_not_create_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = create_repository(root)
            request = TaskRequest(
                repository,
                "在本地分析",
                root / "runs",
                workspace_selection=WorkspaceSelection(mode=WorkspaceMode.LOCAL),
            )
            result = WorkspaceManager().prepare(request, PermissionGrant.safe())
        self.assertEqual("LOCAL_WORKSPACE_READY", result.code)
        self.assertEqual(repository.resolve(), result.workspace_path)

    def test_safe_migration_skips_sensitive_untracked_files_and_keeps_source_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = create_repository(root)
            (repository / "README.md").write_text("changed\n", encoding="utf-8")
            (repository / "notes.txt").write_text("carry me\n", encoding="utf-8")
            (repository / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
            before = WorkspaceManager().snapshot(repository)
            request = TaskRequest(
                repository,
                "迁移改动",
                root / "runs",
                workspace_selection=WorkspaceSelection(include_uncommitted_changes=True),
            )
            result = WorkspaceManager().prepare(request, PermissionGrant.safe())
            after = WorkspaceManager().snapshot(repository)
            self.assertEqual("READY", result.status)
            self.assertEqual("changed\n", (result.workspace_path / "README.md").read_text(encoding="utf-8"))
            self.assertTrue((result.workspace_path / "notes.txt").is_file())
            self.assertFalse((result.workspace_path / ".env").exists())
            self.assertEqual(before, after)

    def test_start_ref_and_branch_creation_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = create_repository(root)
            _git(repository, "branch", "release")
            release_commit = _git(repository, "rev-parse", "release")
            request = TaskRequest(
                repository,
                "从 release 创建工作区",
                root / "runs",
                workspace_selection=WorkspaceSelection(start_ref="release"),
            )
            manager = WorkspaceManager()
            result = manager.prepare(request, PermissionGrant.safe())
            branch = manager.create_branch(result.workspace_path, "agent/fix-order")
            status = manager.status(result.workspace_path)
        self.assertEqual("READY", result.status)
        self.assertEqual(release_commit, result.base_commit)
        self.assertEqual("BRANCH_CREATED", branch["code"])
        self.assertEqual("agent/fix-order", status["branch"])


class ContextTests(unittest.TestCase):
    def test_project_memory_only_records_verified_facts_and_isolated_by_project(self) -> None:
        client = MemoryFakeQdrant()
        writer = VerifiedProjectMemoryWriter(client, FakeEmbeddings())
        blocked = writer.record(
            project_id="project-a",
            task_id="task-unverified",
            repo_commit="commit-a",
            changed_paths=("src/App.java",),
            git_diff="diff --git a/src/App.java b/src/App.java",
            verification={"status": "FAILED", "recipe": "test", "exit_code": 1},
        )
        recorded = writer.record(
            project_id="project-a",
            task_id="task-verified",
            repo_commit="commit-a",
            changed_paths=("src/App.java",),
            git_diff="diff --git a/src/App.java b/src/App.java\n+value",
            verification={"status": "PASSED", "recipe": "test", "exit_code": 0},
        )
        # 重放同一验证结果只 upsert 同一稳定 point，不会制造第二份记忆事实。
        repeated = writer.record(
            project_id="project-a",
            task_id="task-verified",
            repo_commit="commit-a",
            changed_paths=("src/App.java",),
            git_diff="diff --git a/src/App.java b/src/App.java\n+value",
            verification={"status": "PASSED", "recipe": "test", "exit_code": 0},
        )

        retriever = ProjectMemoryRetriever(client, FakeEmbeddings())
        same_project = retriever.search("App", project_id="project-a")
        other_project = retriever.search("App", project_id="project-b")

        self.assertEqual("PROJECT_MEMORY_UNVERIFIED", blocked.code)
        self.assertEqual("PROJECT_MEMORY_RECORDED", recorded.code)
        self.assertEqual("PROJECT_MEMORY_RECORDED", repeated.code)
        self.assertEqual(1, len(client.points["project_memory"]))
        self.assertEqual(1, len(same_project.contexts))
        self.assertEqual("verified_project_memory", same_project.contexts[0].source_type)
        self.assertEqual(0, len(other_project.contexts))

    def test_transient_embedding_operation_retries_with_bounded_backoff(self) -> None:
        attempts = 0

        def operation() -> str:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise APIConnectionError(request=httpx.Request("POST", "https://embedding.invalid"))
            return "ready"

        with patch("repopilot_guard.context.time.sleep") as sleep:
            result = _with_transient_retry(operation)

        self.assertEqual("ready", result)
        self.assertEqual(3, attempts)
        self.assertEqual([call(1.0), call(2.0)], sleep.call_args_list)

    def test_qdrant_point_id_is_a_stable_uuid(self) -> None:
        chunk_id = "a" * 64
        self.assertEqual("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", _qdrant_point_id(chunk_id))

    def test_chunking_is_deterministic_and_tracks_line_numbers(self) -> None:
        content = "# 标题\n第一行\n第二行\n"
        first = split_context(
            content, project_id="project-a", repo_commit="abc", source_type="repository_document", path="README.md", document_id="doc", markdown=True
        )
        second = split_context(
            content, project_id="project-a", repo_commit="abc", source_type="repository_document", path="README.md", document_id="doc", markdown=True
        )
        self.assertEqual(first, second)
        self.assertEqual(1, first[0].line_start)
        self.assertEqual(3, first[0].line_end)

    def test_index_is_incremental_and_retrieval_isolated_by_project_and_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            client = FakeQdrant()
            store = ContextChunkStore(root / "state.sqlite")
            chunks = split_context(
                "class OrderService {}\n",
                project_id="project-a",
                repo_commit="commit-a",
                source_type="code",
                path="OrderService.java",
                document_id="code",
                markdown=False,
            )
            indexer = ContextIndexer(client, FakeEmbeddings(), store)
            self.assertEqual("CONTEXT_INDEXED", indexer.index(chunks).code)
            self.assertEqual("CONTEXT_ALREADY_INDEXED", indexer.index(chunks).code)
            self.assertEqual(1, client.upsert_calls)
            retriever = ContextRetriever(client, FakeEmbeddings())
            found = retriever.search("Order", project_id="project-a", repo_commit="commit-a")
            missing = retriever.search("Order", project_id="project-b", repo_commit="commit-a")
            self.assertEqual(1, len(found.contexts))
            self.assertEqual(0, len(missing.contexts))
            store.close()

    def test_hybrid_retrieval_promotes_exact_path_and_keyword_match_over_vector_rank(self) -> None:
        payload = lambda path, content: {
            "content": content,
            "path": path,
            "line_start": 1,
            "line_end": 1,
            "source_type": "code",
            "document_id": path,
        }
        client = RankedFakeQdrant(
            [
                FakePoint(payload("src/main/java/LegacyCache.java", "class LegacyCache {}"), score=0.99),
                FakePoint(payload("src/main/java/TenantOrderService.java", "class TenantOrderService { void queryTenantOrder() {} }"), score=0.72),
                FakePoint(payload("docs/orders.md", "订单租户过滤设计。"), score=0.65),
            ]
        )

        result = ContextRetriever(client, FakeEmbeddings()).search(
            "tenant order service",
            project_id="project-a",
            repo_commit="commit-a",
            limit=2,
        )

        self.assertEqual("hybrid_vector_lexical_path", result.strategy)
        self.assertEqual(3, result.candidate_count)
        self.assertEqual(8, client.last_limit)
        self.assertEqual("src/main/java/TenantOrderService.java", result.contexts[0].path)
        self.assertGreater(result.contexts[0].lexical_score, result.contexts[1].lexical_score)
        self.assertEqual(0.72, result.contexts[0].vector_score)

    def test_hybrid_retrieval_has_stable_path_tiebreaker(self) -> None:
        context = lambda path: RetrievedContext("class Service {}", 0.9, path, 1, 1, "code", path, vector_score=0.9)
        result = _hybrid_rerank("unmatched", [(context("z/Service.java"), 0), (context("a/Service.java"), 0)], 2)

        self.assertEqual(["a/Service.java", "z/Service.java"], [item.path for item in result])

    def test_embedding_failure_is_blocked_without_index_success(self) -> None:
        chunks = split_context(
            "class OrderService {}\n",
            project_id="project-a",
            repo_commit="commit-a",
            source_type="code",
            path="OrderService.java",
            document_id="code",
            markdown=False,
        )
        result = ContextIndexer(FakeQdrant(), FailingEmbeddings()).index(chunks)
        self.assertEqual("BLOCKED", result.status)
        self.assertEqual("CONTEXT_INDEX_FAILED", result.code)
        self.assertEqual("embedding", result.failure_component)
        self.assertEqual("RuntimeError", result.failure_reason)

    def test_loader_does_not_index_sensitive_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = create_repository(root)
            (repository / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
            chunks, _ = ContextLoader().load_project(
                repository,
                project_id="project-a",
                repo_commit="commit-a",
                permission=PermissionGrant.safe(),
            )
        self.assertNotIn(".env", {chunk.path for chunk in chunks})

    def test_managed_document_import_hides_source_path_and_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "external-requirements.md"
            original = "# 订单需求\n订单查询必须按租户隔离。\n"
            source.write_text(original, encoding="utf-8")
            documents = ManagedDocumentStore(root / "state.sqlite")

            imported = documents.import_document(source, project_id="project-a")
            chunks = documents.chunks_for(imported, project_id="project-a", repo_commit="commit-a")

            self.assertEqual(original, source.read_text(encoding="utf-8"))
            self.assertTrue(imported.managed_path.is_file())
            self.assertNotIn(str(source), chunks[0].path)
            self.assertEqual("uploaded_document", chunks[0].source_type)
            self.assertTrue(chunks[0].path.startswith("uploaded_documents/"))

            imported.managed_path.write_text("externally modified", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "MANAGED_DOCUMENT_INTEGRITY_FAILED"):
                documents.chunks_for(imported, project_id="project-a", repo_commit="commit-a")
