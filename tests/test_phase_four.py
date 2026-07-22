from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

import httpx
from openai import APIConnectionError
from pydantic import ValidationError

from repopilot_guard.context import IndexResult, ProjectMemoryResult, RetrievalResult, RetrievedContext
from repopilot_guard.cancellation import TaskCancellationRegistry
from repopilot_guard.graph import (
    ChangePlan,
    CodingGraphFactory,
    EvidenceReference,
    GraphPreflightChecker,
    GraphRunner,
    ModelUsage,
    OpenAIResearchModel,
    PatchGenerationResult,
    PlanGenerationResult,
    PhaseOnePreflightResult,
    ResearchDecision,
    SqliteCheckpointStore,
    ToolCall,
    _validation_issue_summary,
)
from repopilot_guard.execution import PatchProposal
from repopilot_guard.config import ComponentCheck
from repopilot_guard.models import TaskBudget, TaskRequest, VerificationContract
from repopilot_guard.workspace import WorkspaceManager


def create_java_repository(root: Path) -> Path:
    repository = root / "repository"
    repository.mkdir()
    for args in (("init", "-b", "main"), ("config", "user.name", "RepoPilot Test"), ("config", "user.email", "test@example.invalid")):
        subprocess.run(("git", "-C", str(repository), *args), check=True, capture_output=True)
    source = repository / "src" / "main" / "java" / "com" / "example"
    source.mkdir(parents=True)
    (source / "OrderService.java").write_text("package com.example;\nclass OrderService { void findOrder() {} }\n", encoding="utf-8")
    (repository / "pom.xml").write_text("<project><artifactId>demo</artifactId></project>\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(repository), "add", "."), check=True, capture_output=True)
    subprocess.run(("git", "-C", str(repository), "commit", "-m", "fixture"), check=True, capture_output=True)
    return repository


class ReadyChecker(GraphPreflightChecker):
    def check(self, repository: Path) -> PhaseOnePreflightResult:
        return PhaseOnePreflightResult(True, (ComponentCheck("all", True, "READY", "测试预检通过。"),))


class FakeContextService:
    def ingest(self, workspace: object, project_id: str, permission: object) -> IndexResult:
        return IndexResult("READY", "CONTEXT_INDEXED", "测试索引完成。", indexed_chunks=1)

    def retrieve(self, query: str, project_id: str, repo_commit: str) -> RetrievalResult:
        return RetrievalResult(
            "READY",
            "CONTEXT_RETRIEVED",
            "测试检索完成。",
            (RetrievedContext("class OrderService {}", 0.9, "src/main/java/com/example/OrderService.java", 1, 2, "code", "code"),),
        )


class PlannedResearchModel:
    def __init__(self, calls: tuple[ToolCall, ...] = ()) -> None:
        self.calls = calls
        self.analyze_count = 0
        self.plan_count = 0

    def analyze(self, messages: list[dict[str, str]], tools: tuple[object, ...]) -> ResearchDecision:
        self.analyze_count += 1
        return ResearchDecision("继续收集证据。", self.calls if self.analyze_count == 1 else ())

    def plan(self, messages: list[dict[str, str]], state: object) -> PlanGenerationResult:
        self.plan_count += 1
        return PlanGenerationResult(
            ChangePlan(
                summary="OrderService 的查询路径需要补充权限条件。",
                evidence=[EvidenceReference(source_type="code", path="src/main/java/com/example/OrderService.java", line_start=1, line_end=2, note="查询入口")],
                candidate_files=["src/main/java/com/example/OrderService.java"],
                steps=["在订单查询入口增加权限条件。"],
                verification=["阶段五运行目标测试。"],
            )
        )

    def propose_patch(self, messages: list[dict[str, str]], state: object) -> PatchGenerationResult:
        return PatchGenerationResult(
            PatchProposal(
                summary="测试补丁",
                changes=[{
                    "path": "src/main/java/com/example/OrderService.java",
                    "expected_old_text": "void findOrder() {}",
                    "new_text": "void findOrder() { /* verified */ }",
                }],
            )
        )


class LoopingResearchModel(PlannedResearchModel):
    def analyze(self, messages: list[dict[str, str]], tools: tuple[object, ...]) -> ResearchDecision:
        self.analyze_count += 1
        return ResearchDecision("继续搜索。", (ToolCall("search_code", {"query": "OrderService"}),))


class FailingProjectMemoryWriter:
    def record(self, **_: object) -> ProjectMemoryResult:
        return ProjectMemoryResult("BLOCKED", "PROJECT_MEMORY_INDEX_FAILED", "Qdrant 不可用。", failure_component="qdrant")


class CapturingJsonModel:
    """模拟 OpenAI-compatible JSON Mode，并保存最终补丁提示供协议断言。"""

    def __init__(self) -> None:
        self.messages: list[dict[str, str]] = []

    def bind(self, **_: object) -> "CapturingJsonModel":
        return self

    def invoke(self, messages: list[dict[str, str]]) -> object:
        self.messages = messages
        return type(
            "Response",
            (),
            {
                "content": (
                    '{"summary":"补充测试","changes":[{"path":"src/test/java/com/repopilot/demo/OrderServiceTest.java",'
                    '"expected_old_text":"old","new_text":"new"}],"recipe":"targeted_test",'
                    '"test_class":"com.repopilot.demo.OrderServiceTest"}'
                )
            },
        )()


class RepairingJsonModel(CapturingJsonModel):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[list[dict[str, str]]] = []

    def invoke(self, messages: list[dict[str, str]]) -> object:
        self.calls.append(messages)
        if len(self.calls) == 1:
            return type("Response", (), {"content": '{"summary":"无效补丁","changes":[]}'})()
        return super().invoke(messages)


class RepairingPlanJsonModel:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    def bind(self, **_: object) -> "RepairingPlanJsonModel":
        return self

    def invoke(self, messages: list[dict[str, str]]) -> object:
        self.calls.append(messages)
        target_test_class = '"com.repopilot.demo.OrderServiceTest"' if len(self.calls) == 1 else "null"
        content = (
            '{"summary":"修复订单校验","evidence":[],"candidate_files":[],"steps":[],'
            '"verification":[],"assumptions":[],"risks":[],"verification_recipe":"test",'
            f'"target_test_class":{target_test_class}'
            "}"
        )
        return type("Response", (), {"content": content})()


class ContractRepairingPlanJsonModel(RepairingPlanJsonModel):
    def invoke(self, messages: list[dict[str, str]]) -> object:
        self.calls.append(messages)
        recipe = "targeted_test" if len(self.calls) == 1 else "test"
        target = '"com.repopilot.demo.OrderMapperXmlTest"' if len(self.calls) == 1 else "null"
        content = (
            '{"summary":"修复分页 SQL","evidence":[],"candidate_files":[],"steps":[],'
            '"verification":[],"assumptions":[],"risks":[],'
            f'"verification_recipe":"{recipe}","target_test_class":{target}'
            "}"
        )
        return type("Response", (), {"content": content})()


class TransientAnalyzeModel:
    def __init__(self) -> None:
        self.attempts = 0

    def bind_tools(self, tools: list[object]) -> "TransientAnalyzeModel":
        return self

    def invoke(self, messages: list[dict[str, str]]) -> object:
        self.attempts += 1
        if self.attempts < 3:
            raise APIConnectionError(request=httpx.Request("POST", "https://chat.invalid"))
        return type("Response", (), {"content": "分析完成", "tool_calls": []})()


class UsageMetadataModel:
    def bind_tools(self, tools: list[object]) -> "UsageMetadataModel":
        return self

    def invoke(self, messages: list[dict[str, str]]) -> object:
        return type(
            "Response",
            (),
            {
                "content": "已获取证据。",
                "tool_calls": [],
                "usage_metadata": {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20},
            },
        )()


class OverBudgetResearchModel(PlannedResearchModel):
    def analyze(self, messages: list[dict[str, str]], tools: tuple[object, ...]) -> ResearchDecision:
        self.analyze_count += 1
        return ResearchDecision("已消耗预算。", usage=ModelUsage(input_tokens=8, output_tokens=5, total_tokens=13, reported=True))


class OverCostBudgetResearchModel(PlannedResearchModel):
    def analyze(self, messages: list[dict[str, str]], tools: tuple[object, ...]) -> ResearchDecision:
        self.analyze_count += 1
        return ResearchDecision("已消耗成本预算。", usage=ModelUsage(input_tokens=8, output_tokens=5, total_tokens=13, reported=True, estimated_cost=0.0002, currency="CNY"))


class PhaseFourGraphTests(unittest.TestCase):
    def test_project_memory_failure_does_not_downgrade_verified_repair(self) -> None:
        factory = CodingGraphFactory(ReadyChecker(), project_memory_writer=FailingProjectMemoryWriter())
        result = factory._review(
            {
                "thread_id": "memory-thread",
                "task_id": "memory-task",
                "project_id": "project-a",
                "base_commit": "commit-a",
                "git_diff": "diff --git a/src/App.java b/src/App.java",
                "patch_result": {"paths": ["src/App.java"]},
                "verification_result": {"status": "PASSED", "recipe": "test", "exit_code": 0},
                "tool_events": [],
            }
        )

        self.assertEqual("PASSED", result["verdict"])
        self.assertIn(
            "PROJECT_MEMORY_INDEX_FAILED",
            {str(event.get("code")) for event in result["tool_events"]},
        )

    def test_cancelled_thread_stops_before_workspace_and_model_research(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = create_java_repository(root)
            store = SqliteCheckpointStore(root / "state.sqlite")
            cancellations = TaskCancellationRegistry()
            model = PlannedResearchModel()
            graph = CodingGraphFactory(
                ReadyChecker(),
                context_service=FakeContextService(),
                research_model=model,
                cancellation_registry=cancellations,
            ).create(store.checkpointer)
            runner = GraphRunner(graph, cancellations)
            cancellations.request("cancelled-thread", "用户在研究前停止任务")

            result = runner.run(TaskRequest(repository, "请分析订单模块", root / "runs"), "cancelled-thread")

            store.close()
        self.assertEqual("BLOCKED", result.status)
        self.assertEqual(0, model.analyze_count)
        self.assertIn(
            "TASK_CANCELLATION_OBSERVED",
            {str(event.get("code")) for event in result.state["tool_events"]},
        )

    def test_analyze_retries_transient_chat_failures_with_bounded_backoff(self) -> None:
        model = TransientAnalyzeModel()
        with patch("repopilot_guard.graph.time.sleep") as sleep:
            result = OpenAIResearchModel(model=model).analyze([], ())

        self.assertEqual("分析完成", result.content)
        self.assertEqual(3, model.attempts)
        self.assertEqual([call(1.0), call(2.0)], sleep.call_args_list)

    def test_model_usage_uses_provider_metadata_and_optional_local_pricing(self) -> None:
        research_model = OpenAIResearchModel(model=UsageMetadataModel())
        research_model._pricing = (2.0, 4.0, "CNY")

        result = research_model.analyze([], ())

        self.assertTrue(result.usage.reported)
        self.assertEqual((12, 8, 20), (result.usage.input_tokens, result.usage.output_tokens, result.usage.total_tokens))
        self.assertEqual(0.000056, result.usage.estimated_cost)
        self.assertEqual("CNY", result.usage.currency)

    def test_node_instrumentation_records_duration_without_input_content(self) -> None:
        instrumented = CodingGraphFactory._instrument_node("INTAKE", lambda state: {"status": "PREFLIGHT"})

        result = instrumented({"tool_events": []})

        event = result["tool_events"][-1]
        self.assertEqual("NODE_COMPLETED", event["type"])
        self.assertEqual("INTAKE", event["node"])
        self.assertIsInstance(event["duration_ms"], int)
        self.assertGreaterEqual(event["duration_ms"], 0)

    def test_token_budget_blocks_before_plan_and_records_actual_usage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            model = OverBudgetResearchModel()
            runner, store = self._runner(root / "state.sqlite", model)
            result = runner.run(
                TaskRequest(
                    create_java_repository(root),
                    "定位订单问题",
                    root / "runs",
                    budget=TaskBudget(max_total_tokens=10),
                )
            )
            store.close()

        self.assertEqual("BLOCKED", result.status)
        self.assertEqual(0, model.plan_count)
        self.assertIn("MODEL_TOKEN_BUDGET_EXCEEDED", {str(event.get("code")) for event in result.state["tool_events"]})
        self.assertIn("MODEL_USAGE_REPORTED", {str(event.get("code")) for event in result.state["tool_events"]})

    def test_token_budget_fails_closed_when_provider_does_not_return_usage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            model = PlannedResearchModel()
            runner, store = self._runner(root / "state.sqlite", model)
            result = runner.run(
                TaskRequest(
                    create_java_repository(root),
                    "定位订单问题",
                    root / "runs",
                    budget=TaskBudget(max_total_tokens=10),
                )
            )
            store.close()

        self.assertEqual("BLOCKED", result.status)
        self.assertEqual(0, model.plan_count)
        self.assertIn("MODEL_USAGE_UNAVAILABLE", {str(event.get("code")) for event in result.state["tool_events"]})

    def test_cost_budget_blocks_before_plan_and_keeps_frozen_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            model = OverCostBudgetResearchModel()
            runner, store = self._runner(root / "state.sqlite", model)
            result = runner.run(
                TaskRequest(
                    create_java_repository(root),
                    "定位订单问题",
                    root / "runs",
                    budget=TaskBudget(max_estimated_cost=0.0001, currency="CNY"),
                )
            )
            store.close()

        self.assertEqual("BLOCKED", result.status)
        self.assertEqual(0, model.plan_count)
        self.assertEqual(0.0001, result.state["budget_snapshot"]["max_estimated_cost"])
        self.assertIn("MODEL_COST_BUDGET_EXCEEDED", {str(event.get("code")) for event in result.state["tool_events"]})

    def test_plan_repairs_trusted_verification_contract_mismatch(self) -> None:
        model = ContractRepairingPlanJsonModel()
        result = OpenAIResearchModel(model=model).plan(
            [],
            {"verification_contract": {"recipe": "test", "target_test_class": None}},
        )

        self.assertEqual(2, result.attempts)
        self.assertEqual("test", result.plan.verification_recipe.value)
        self.assertEqual(2, len(model.calls))
        self.assertIn("trusted_contract_mismatch", model.calls[1][-1]["content"])

    def test_plan_contract_repairs_invalid_recipe_and_test_class_pair(self) -> None:
        model = RepairingPlanJsonModel()
        result = OpenAIResearchModel(model=model).plan([], {})

        self.assertEqual(2, result.attempts)
        self.assertEqual("test", result.plan.verification_recipe.value)
        self.assertIsNone(result.plan.target_test_class)
        self.assertEqual(2, len(model.calls))
        self.assertIn("value_error", model.calls[1][-1]["content"])

    def test_change_plan_rejects_invalid_recipe_and_test_class_pair(self) -> None:
        with self.assertRaises(ValidationError):
            ChangePlan(
                summary="无效计划",
                verification_recipe="test",
                target_test_class="com.repopilot.demo.OrderServiceTest",
            )

    def test_graph_blocks_fake_model_that_violates_trusted_verification_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = create_java_repository(root)
            runner, store = self._runner(root / "state.sqlite", PlannedResearchModel())
            try:
                result = runner.run(
                    TaskRequest(
                        repository,
                        "修复订单查询",
                        root / "output",
                        verification_contract=VerificationContract("compile"),
                    ),
                    "trusted-contract-thread",
                )
            finally:
                store.close()

        self.assertEqual("BLOCKED", result.verdict)
        self.assertEqual("模型计划违反任务验证契约，未进入审批或执行。", result.state["error_summary"])

    def test_validation_diagnostic_excludes_model_input_value(self) -> None:
        with self.assertRaises(ValidationError) as captured:
            PatchProposal.model_validate({"summary": "无效补丁", "changes": []})

        issues = _validation_issue_summary(captured.exception)

        self.assertEqual([{"field": "changes", "rule": "too_short"}], issues)

    def test_patch_prompt_includes_approved_maven_recipe_and_target_test(self) -> None:
        model = CapturingJsonModel()
        research_model = OpenAIResearchModel(model=model)
        result = research_model.propose_patch(
            [],
            {
                "plan": ChangePlan(
                    summary="补充订单测试。",
                    verification_recipe="targeted_test",
                    target_test_class="com.repopilot.demo.OrderServiceTest",
                ).model_dump(mode="json")
            },
        )

        self.assertEqual("targeted_test", result.proposal.recipe.value)
        self.assertEqual("com.repopilot.demo.OrderServiceTest", result.proposal.test_class)
        self.assertEqual(1, result.attempts)
        self.assertIn('"verification_recipe": "targeted_test"', model.messages[-1]["content"])
        self.assertIn('"target_test_class": "com.repopilot.demo.OrderServiceTest"', model.messages[-1]["content"])

    def test_patch_contract_is_repaired_once_with_sanitized_issues(self) -> None:
        model = RepairingJsonModel()
        result = OpenAIResearchModel(model=model).propose_patch(
            [],
            {
                "plan": ChangePlan(
                    summary="补充订单测试。",
                    candidate_files=["src/test/java/com/repopilot/demo/OrderServiceTest.java"],
                    verification_recipe="targeted_test",
                    target_test_class="com.repopilot.demo.OrderServiceTest",
                ).model_dump(mode="json")
            },
        )

        self.assertEqual(2, result.attempts)
        self.assertEqual(({"field": "changes", "rule": "too_short"},), result.repaired_issues)
        self.assertEqual(2, len(model.calls))
        self.assertIn('"field": "changes"', model.calls[1][-1]["content"])
        self.assertNotIn("无效补丁", model.calls[1][-1]["content"])

    def _runner(self, database: Path, model: PlannedResearchModel) -> tuple[GraphRunner, SqliteCheckpointStore]:
        store = SqliteCheckpointStore(database)
        graph = CodingGraphFactory(ReadyChecker(), context_service=FakeContextService(), research_model=model).create(store.checkpointer)
        return GraphRunner(graph), store

    def test_graph_runs_read_only_research_then_pauses_and_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = create_java_repository(root)
            manager = WorkspaceManager()
            before = manager.snapshot(repository)
            runner, store = self._runner(root / "state.sqlite", PlannedResearchModel((ToolCall("read_file", {"path": "src/main/java/com/example/OrderService.java"}),)))
            result = runner.run(TaskRequest(repository, "订单查询缺少权限", root / "runs"), "phase-four-thread")
            after = manager.snapshot(repository)
            self.assertEqual("WAITING_APPROVAL", result.status)
            self.assertTrue(result.pending_approval)
            self.assertEqual("PLAN_REVIEW", result.state["pending_approval_action"])
            self.assertEqual(before, after)
            self.assertIn("src/main/java/com/example/OrderService.java", result.state["candidate_files"])
            self.assertEqual("phase-four-thread", result.state["thread_id"])
            self.assertEqual("CONTEXT_BROKER_READY", next(event["code"] for event in result.state["tool_events"] if event["type"] == "CONTEXT_BROKER_ASSEMBLED"))
            self.assertEqual(str(result.state["base_commit"]), result.state["context_snapshot"]["repo_commit"])
            self.assertIn("read_file", result.state["context_snapshot"]["bound_tool_ids"])
            self.assertIn("read_file", result.state["context_snapshot"]["capability_ids"])
            completed = runner.resume("phase-four-thread", approved=True)
            store.close()
        self.assertEqual("WAITING_APPROVAL", completed.status)
        self.assertEqual("EXECUTION_REVIEW", completed.state["pending_approval_action"])

    def test_unknown_tool_is_audited_and_never_becomes_shell_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            runner, store = self._runner(root / "state.sqlite", PlannedResearchModel((ToolCall("run_shell", {"command": "del /s"}),)))
            result = runner.run(TaskRequest(create_java_repository(root), "检查风险", root / "runs"))
            store.close()
        events = [event for event in result.state["tool_events"] if event.get("type") == "TOOL_CALL"]
        self.assertEqual("TOOL_NOT_ALLOWLISTED", events[0]["code"])
        self.assertEqual("BLOCKED", events[0]["status"])

    def test_research_loop_is_bounded_before_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            model = LoopingResearchModel()
            runner, store = self._runner(root / "state.sqlite", model)
            result = runner.run(TaskRequest(create_java_repository(root), "定位订单问题", root / "runs"))
            store.close()
        self.assertEqual("WAITING_APPROVAL", result.status)
        self.assertLessEqual(result.state["research_rounds"], 6)
        self.assertLessEqual(result.state["tool_call_count"], 12)
        self.assertIn("RESEARCH_LIMIT_REACHED", {event["type"] for event in result.state["tool_events"]})

    def test_plan_revision_returns_to_plan_and_requires_a_new_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            model = PlannedResearchModel()
            runner, store = self._runner(root / "state.sqlite", model)
            initial = runner.run(TaskRequest(create_java_repository(root), "定位订单问题", root / "runs"), "revision-thread")
            revised = runner.resume("revision-thread", decision="revise", comment="不要修改 Controller，请补充 Service 层证据。")
            store.close()
        self.assertEqual("WAITING_APPROVAL", initial.status)
        self.assertEqual("WAITING_APPROVAL", revised.status)
        self.assertEqual("PLAN_REVIEW", revised.state["pending_approval_action"])
        self.assertEqual(1, revised.state["plan_revision"])
        self.assertEqual(2, model.plan_count)
        self.assertIn("PLAN_REVISION_REQUESTED", {event["type"] for event in revised.state["tool_events"]})
