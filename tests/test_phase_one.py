from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from repopilot_guard.config import AppSettings, ComponentCheck, sanitized_settings_error
from repopilot_guard.graph import (
    CodingGraphFactory,
    GraphPreflightChecker,
    GraphRunner,
    PhaseOnePreflightResult,
    SqliteCheckpointStore,
    create_live_graph,
)
from repopilot_guard.models import TaskBudget, TaskRequest
from repopilot_guard.providers import OpenAICompatibleProvider
from repopilot_guard.qdrant_bootstrap import COLLECTION_NAMES, PAYLOAD_INDEXES, QdrantBootstrapper


class FakeCollection:
    def __init__(self) -> None:
        self.payload_schema: dict[str, object] = {}


class FakeQdrantClient:
    def __init__(self) -> None:
        self.collections: dict[str, FakeCollection] = {}

    def collection_exists(self, collection_name: str) -> bool:
        return collection_name in self.collections

    def create_collection(self, collection_name: str, vectors_config: object) -> None:
        self.collections[collection_name] = FakeCollection()

    def get_collection(self, collection_name: str) -> FakeCollection:
        return self.collections[collection_name]

    def create_payload_index(self, collection_name: str, field_name: str, field_schema: object) -> None:
        self.collections[collection_name].payload_schema[field_name] = field_schema


class ReadyPreflightChecker(GraphPreflightChecker):
    def check(self, repository: Path) -> PhaseOnePreflightResult:
        return PhaseOnePreflightResult(
            ready=True,
            checks=(ComponentCheck("all", True, "READY", "测试预检通过。"),),
        )


class BlockedPreflightChecker(GraphPreflightChecker):
    def check(self, repository: Path) -> PhaseOnePreflightResult:
        return PhaseOnePreflightResult(
            ready=False,
            checks=(ComponentCheck("all", False, "BLOCKED", "测试预检阻断。"),),
        )


class AppSettingsTests(unittest.TestCase):
    def test_task_budget_is_optional_but_validates_positive_token_limit(self) -> None:
        with patch.dict(os.environ, {"REPOPILOT_TASK_MAX_TOTAL_TOKENS": "12000", "REPOPILOT_TASK_MAX_ESTIMATED_COST": "0.5"}, clear=True):
            budget = AppSettings(_env_file=None).task_budget()

        self.assertEqual(12000, budget.max_total_tokens)
        self.assertEqual(0.5, budget.max_estimated_cost)
        self.assertEqual("CNY", budget.currency)

    def test_request_budget_cannot_relax_server_budget_or_mix_cost_currencies(self) -> None:
        policy = TaskBudget(max_total_tokens=100, max_estimated_cost=1.0, currency="CNY")

        effective = TaskBudget(max_total_tokens=500, max_estimated_cost=3.0, currency="CNY").restricted_by(policy)

        self.assertEqual(100, effective.max_total_tokens)
        self.assertEqual(1.0, effective.max_estimated_cost)
        with self.assertRaisesRegex(ValueError, "currency conflicts"):
            TaskBudget(max_estimated_cost=0.5, currency="USD").restricted_by(policy)

    def test_missing_configuration_returns_blocked_without_secret_value(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = AppSettings(_env_file=None)

        check = settings.chat_check()
        self.assertFalse(check.ready)
        self.assertEqual("BLOCKED", check.status)
        self.assertIn("REPOPILOT_CHAT_API_KEY", check.missing_fields)

    def test_secret_is_not_exposed_by_audit_result(self) -> None:
        secret = "super-secret-token"
        with patch.dict(
            os.environ,
            {
                "REPOPILOT_CHAT_BASE_URL": "https://example.invalid/v1",
                "REPOPILOT_CHAT_API_KEY": secret,
                "REPOPILOT_CHAT_MODEL": "test-model",
            },
            clear=True,
        ):
            settings = AppSettings(_env_file=None)

        self.assertNotIn(secret, str(settings.chat_check().to_dict()))
        self.assertNotIn(secret, str(sanitized_settings_error().to_dict()))

    def test_blank_api_key_is_treated_as_missing_configuration(self) -> None:
        with patch.dict(
            os.environ,
            {
                "REPOPILOT_CHAT_BASE_URL": "https://api.moonshot.cn/v1",
                "REPOPILOT_CHAT_API_KEY": "",
                "REPOPILOT_CHAT_MODEL": "kimi-k3",
            },
            clear=True,
        ):
            check = AppSettings(_env_file=None).chat_check()

        self.assertFalse(check.ready)
        self.assertIn("REPOPILOT_CHAT_API_KEY", check.missing_fields)

    def test_invalid_embedding_dimensions_is_sanitized_as_blocked(self) -> None:
        with patch.dict(os.environ, {"REPOPILOT_EMBEDDING_DIMENSIONS": "invalid"}, clear=True):
            with self.assertRaises(ValidationError):
                AppSettings(_env_file=None)
        self.assertEqual("BLOCKED", sanitized_settings_error().status)

    def test_openai_compatible_provider_only_constructs_clients(self) -> None:
        with patch.dict(
            os.environ,
            {
                "REPOPILOT_CHAT_BASE_URL": "https://example.invalid/v1",
                "REPOPILOT_CHAT_API_KEY": "test-key",
                "REPOPILOT_CHAT_MODEL": "test-chat",
                "REPOPILOT_EMBEDDING_BASE_URL": "https://example.invalid/v1",
                "REPOPILOT_EMBEDDING_API_KEY": "test-key",
                "REPOPILOT_EMBEDDING_MODEL": "test-embedding",
                "REPOPILOT_EMBEDDING_DIMENSIONS": "1536",
            },
            clear=True,
        ):
            provider = OpenAICompatibleProvider(AppSettings(_env_file=None))
            chat_model = provider.create_chat_model()
            embeddings = provider.create_embeddings()

        self.assertEqual("ChatOpenAI", type(chat_model).__name__)
        self.assertEqual("OpenAIEmbeddings", type(embeddings).__name__)
        self.assertFalse(embeddings.check_embedding_ctx_length)


class QdrantBootstrapperTests(unittest.TestCase):
    def test_bootstrap_is_idempotent_and_keeps_existing_collections(self) -> None:
        client = FakeQdrantClient()
        bootstrapper = QdrantBootstrapper(client, embedding_dimensions=1536)

        first = bootstrapper.bootstrap()
        second = bootstrapper.bootstrap()

        self.assertEqual(set(COLLECTION_NAMES), set(first.collections_created))
        self.assertEqual((), second.collections_created)
        self.assertEqual(set(COLLECTION_NAMES), set(second.collections_existing))
        self.assertEqual((), second.indexes_created)
        for collection in client.collections.values():
            self.assertEqual(set(PAYLOAD_INDEXES), set(collection.payload_schema))


class CodingGraphTests(unittest.TestCase):
    def test_live_graph_starts_without_model_configuration_and_fails_closed_in_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(("git", "-C", str(repository), "init", "-b", "main"), check=True, capture_output=True)
            subprocess.run(("git", "-C", str(repository), "config", "user.name", "RepoPilot Test"), check=True, capture_output=True)
            subprocess.run(("git", "-C", str(repository), "config", "user.email", "test@example.invalid"), check=True, capture_output=True)
            (repository / "pom.xml").write_text("<project />", encoding="utf-8")
            (repository / "src" / "main" / "java").mkdir(parents=True)
            subprocess.run(("git", "-C", str(repository), "add", "."), check=True, capture_output=True)
            subprocess.run(("git", "-C", str(repository), "commit", "-m", "fixture"), check=True, capture_output=True)
            database_path = root / "state.sqlite"

            with (
                patch.dict(os.environ, {"PATH": os.environ["PATH"]}, clear=True),
                patch("repopilot_guard.plugins.PluginRegistry", return_value=None),
            ):
                settings = AppSettings(_env_file=None, state_db_path=database_path)
                store = SqliteCheckpointStore(database_path)
                try:
                    runner = GraphRunner(create_live_graph(settings, store.checkpointer))
                    result = runner.run(
                        TaskRequest(repository=repository, description="检查未配置冷启动", output_root=root / "runs"),
                        thread_id="thread-missing-configuration",
                    )
                finally:
                    store.close()

        self.assertEqual("BLOCKED", result.status)
        self.assertIn("MISSING_CONFIGURATION", str(result.state["tool_events"]))
        self.assertNotIn("PLAN_GENERATED", str(result.state["tool_events"]))

    def test_same_thread_can_interrupt_then_resume_from_sqlite_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(("git", "-C", str(repository), "init", "-b", "main"), check=True, capture_output=True)
            subprocess.run(("git", "-C", str(repository), "config", "user.name", "RepoPilot Test"), check=True, capture_output=True)
            subprocess.run(("git", "-C", str(repository), "config", "user.email", "test@example.invalid"), check=True, capture_output=True)
            (repository / "pom.xml").write_text("<project />", encoding="utf-8")
            (repository / "src" / "main" / "java").mkdir(parents=True)
            subprocess.run(("git", "-C", str(repository), "add", "."), check=True, capture_output=True)
            subprocess.run(("git", "-C", str(repository), "commit", "-m", "fixture"), check=True, capture_output=True)
            request = TaskRequest(repository=repository, description="生成第一阶段报告", output_root=root / "runs")
            database_path = root / "state.sqlite"

            store = SqliteCheckpointStore(database_path)
            runner = GraphRunner(CodingGraphFactory(ReadyPreflightChecker()).create(store.checkpointer))
            interrupted = runner.run(request, thread_id="thread-resume")
            store.close()

            self.assertEqual("WAITING_APPROVAL", interrupted.status)
            self.assertTrue(interrupted.pending_approval)
            self.assertEqual("PLAN_APPROVAL_REQUIRED", interrupted.interrupts[0]["type"])

            resumed_store = SqliteCheckpointStore(database_path)
            resumed_runner = GraphRunner(CodingGraphFactory(ReadyPreflightChecker()).create(resumed_store.checkpointer))
            completed = resumed_runner.resume("thread-resume", approved=True)
            resumed_store.close()

        self.assertEqual("WAITING_APPROVAL", completed.status)
        self.assertEqual("EXECUTION_REVIEW", completed.state["pending_approval_action"])
        self.assertTrue(completed.pending_approval)
        self.assertEqual("EXECUTION_APPROVAL_REQUIRED", completed.interrupts[0]["type"])

    def test_blocked_graph_never_reaches_approval_or_passed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repository"
            repository.mkdir()
            request = TaskRequest(repository=repository, description="生成第一阶段报告", output_root=root / "runs")
            store = SqliteCheckpointStore(root / "state.sqlite")
            runner = GraphRunner(CodingGraphFactory(BlockedPreflightChecker()).create(store.checkpointer))
            result = runner.run(request, thread_id="thread-blocked")
            self.assertEqual("BLOCKED", result.status)
            self.assertEqual("BLOCKED", result.verdict)
            self.assertFalse(result.pending_approval)
            self.assertNotIn("PASSED", str(result.state))
            self.assertFalse(result.interrupts)
            with self.assertRaisesRegex(ValueError, "NO_PENDING_APPROVAL"):
                runner.resume("thread-blocked", approved=True)
            store.close()
