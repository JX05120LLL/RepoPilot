from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from repopilot_guard.capabilities import (
    CapabilityDescriptor,
    CapabilityKind,
    CapabilityRegistry,
    CapabilityRisk,
    CapabilityScope,
)
from repopilot_guard.context import RetrievalResult, RetrievedContext
from repopilot_guard.context_broker import ContextBroker, ContextBudget
from repopilot_guard.permissions import PermissionGrant
from repopilot_guard.skills import SkillRegistry


def _write_skill(root: Path, name: str, description: str, body: str, *, disabled: bool = False) -> Path:
    path = root / ".agents" / "skills" / name
    path.mkdir(parents=True)
    disable = "true" if disabled else "false"
    skill = path / "SKILL.md"
    skill.write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"disable-model-invocation: {disable}\n"
        "allowed-tools:\n"
        "  - read_file\n"
        "---\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return skill


def _retrieval() -> RetrievalResult:
    return RetrievalResult(
        "READY",
        "CONTEXT_RETRIEVED",
        "测试检索完成。",
        (
            RetrievedContext("class OrderService { void query() {} }", 0.95, "src/main/java/OrderService.java", 3, 5, "code", "order-service"),
            RetrievedContext("# 订单权限\n查询必须按租户过滤。", 0.87, "docs/orders.md", 1, 2, "repository_document", "orders"),
        ),
    )


class ContextBrokerTests(unittest.TestCase):
    def test_assembles_budgeted_untrusted_context_and_freezes_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "AGENTS.md").write_text("只在 Service 层实现租户过滤。", encoding="utf-8")
            _write_skill(root, "java-orders", "Java 订单 Maven 维护", "优先检查 Service 和对应单元测试。")
            _write_skill(root, "hidden-skill", "不应自动选择", "不能进入模型。", disabled=True)
            capabilities = CapabilityRegistry(
                (
                    CapabilityDescriptor("read_file", "read_file", "读取文件。", CapabilityKind.BUILTIN_TOOL, CapabilityScope.BUNDLED, "test", frozenset({CapabilityRisk.READ})),
                    CapabilityDescriptor("mcp__docs__search", "search", "远程文档搜索。", CapabilityKind.MCP_TOOL, CapabilityScope.PROJECT, "mcp:docs", frozenset({CapabilityRisk.NETWORK})),
                )
            )
            result = ContextBroker(
                capabilities=capabilities,
                budget=ContextBudget(total_chars=1_600, retrieval_chars=500, skill_catalog_chars=500, skill_instruction_chars=500, project_rule_chars=300),
            ).assemble(
                task_description="修复订单 Java Service 的租户权限 @java-orders",
                project_id="orders",
                repo_commit="abc123",
                workspace_root=root,
                retrieval=_retrieval(),
                permission=PermissionGrant.safe(),
            )

            self.assertEqual("CONTEXT_BROKER_READY", result.code)
            self.assertLessEqual(len(result.model_message), 1_600)
            self.assertIn("不可信项目规则", result.model_message)
            self.assertIn("不可信 Skill 指令", result.model_message)
            self.assertIn("不可信 RAG 片段", result.model_message)
            self.assertNotIn("hidden-skill", result.model_message)
            self.assertIn("read_file", result.snapshot.capability_ids)
            self.assertIn("skill__java-orders", result.snapshot.capability_ids)
            self.assertNotIn("mcp__docs__search", result.snapshot.capability_ids)
            self.assertEqual(("java-orders",), tuple(item["name"] for item in result.snapshot.selected_skills))
            self.assertEqual(
                {"project_rule", "skill", "code", "repository_document"},
                {source.source_type for source in result.snapshot.sources},
            )
            serialized = result.snapshot.to_dict()
            self.assertNotIn("优先检查 Service", str(serialized))
            self.assertNotIn("class OrderService", str(serialized))

    def test_changed_skill_is_not_loaded_after_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skill_path = _write_skill(root, "java-orders", "Java 订单维护", "初始正文")
            registry = SkillRegistry.discover(project_root=root)
            skill_path.write_text(skill_path.read_text(encoding="utf-8") + "\n被篡改的正文", encoding="utf-8")

            result = ContextBroker(skill_registry=registry).assemble(
                task_description="请使用 @java-orders 修复订单",
                project_id="orders",
                repo_commit="abc123",
                workspace_root=root,
                retrieval=_retrieval(),
                permission=PermissionGrant.safe(),
            )

            self.assertIn("SKILL_CHANGED_AFTER_DISCOVERY", result.issues)
            self.assertEqual((), result.snapshot.selected_skills)
            self.assertNotIn("被篡改的正文", result.model_message)

    def test_catalog_and_retrieval_are_truncated_without_losing_budget_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _write_skill(root, "java-orders", "Java 订单维护", "正文" * 500)
            retrieval = RetrievalResult(
                "READY",
                "CONTEXT_RETRIEVED",
                "测试检索完成。",
                (RetrievedContext("代码" * 2_000, 0.9, "OrderService.java", 1, 1, "code", "order"),),
            )
            result = ContextBroker(
                budget=ContextBudget(total_chars=700, retrieval_chars=600, skill_catalog_chars=300, skill_instruction_chars=300, project_rule_chars=100),
            ).assemble(
                task_description="@java-orders 修复订单",
                project_id="orders",
                repo_commit="abc123",
                workspace_root=root,
                retrieval=retrieval,
                permission=PermissionGrant.safe(),
            )

            self.assertLessEqual(len(result.model_message), 700)
            self.assertGreater(result.snapshot.omitted_items, 0)
            self.assertIn("[上下文已按预算截断]", result.model_message)

    def test_explicit_task_attachment_precedes_vector_retrieval_and_is_auditable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            attachment = RetrievedContext(
                "订单查询必须按租户过滤。",
                1.0,
                "uploaded_documents/requirements.md",
                1,
                1,
                "task_attachment",
                "a" * 64,
            )
            result = ContextBroker(
                budget=ContextBudget(
                    total_chars=1_000,
                    retrieval_chars=400,
                    attached_document_chars=400,
                    skill_catalog_chars=100,
                    skill_instruction_chars=100,
                    project_rule_chars=100,
                )
            ).assemble(
                task_description="修复订单查询权限",
                project_id="orders",
                repo_commit="abc123",
                workspace_root=root,
                retrieval=_retrieval(),
                permission=PermissionGrant.safe(),
                attached_contexts=(attachment,),
            )

            self.assertIn("用户显式任务附件", result.model_message)
            self.assertLess(
                result.model_message.index("订单查询必须按租户过滤。"),
                result.model_message.index("class OrderService"),
            )
            self.assertIn("task_attachment", {source.source_type for source in result.snapshot.sources})
            self.assertNotIn("订单查询必须按租户过滤。", str(result.snapshot.to_dict()))


if __name__ == "__main__":
    unittest.main()
