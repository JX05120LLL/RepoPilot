from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from repopilot_guard.subagents import SubagentCoordinator, SubagentSpec


class SubagentCoordinatorTests(unittest.TestCase):
    def test_simple_task_stays_with_the_parent_agent(self) -> None:
        result = SubagentCoordinator().run(
            task_description="说明 UserService 的职责",
            workspace_root=Path.cwd(),
            candidate_files=[],
        )

        self.assertEqual("READY", result.status)
        self.assertEqual("SUBAGENTS_NOT_REQUIRED", result.code)
        self.assertEqual((), result.findings)

    def test_complex_task_runs_three_safe_readonly_agents_in_parallel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "src/main/java/com/example"
            source.mkdir(parents=True)
            (root / "pom.xml").write_text("<project><modelVersion>4.0.0</modelVersion></project>", encoding="utf-8")
            (source / "OrderService.java").write_text(
                "class OrderService { void findOrder() {} }\n", encoding="utf-8"
            )
            tests = root / "src/test/java/com/example"
            tests.mkdir(parents=True)
            (tests / "OrderServiceTest.java").write_text("class OrderServiceTest { @Test void findsOrder() {} }\n", encoding="utf-8")
            (root / ".env").write_text("API_KEY=must-not-leak\n", encoding="utf-8")

            result = SubagentCoordinator().run(
                task_description="同时检查订单查询 Service、测试覆盖和 Maven 构建链路，定位多个模块之间的权限校验问题。",
                workspace_root=root,
                candidate_files=["src/main/java/com/example/OrderService.java", "pom.xml", "src/test/java/com/example/OrderServiceTest.java"],
            )

        self.assertEqual("READY", result.status)
        self.assertEqual("SUBAGENTS_COMPLETED", result.code)
        self.assertEqual({"repository_mapper", "implementation_researcher", "verification_researcher"}, {item.role for item in result.findings})
        self.assertTrue(all(item.status == "READY" for item in result.findings))
        self.assertTrue(all(item.to_dict()["permission_mode"] == "safe" for item in result.findings))
        serialized = str(result.to_dict())
        self.assertNotIn("must-not-leak", serialized)
        self.assertIn("OrderService.java", serialized)


class DeclarativeSubagentSpecTests(unittest.TestCase):
    def test_run_specs_executes_declared_roles(self) -> None:
        def locate(tools: object) -> tuple[str, list[dict[str, object]]]:
            return ("自定义子 Agent 完成", [{"source_type": "custom", "path": "src/X.java", "line_start": 1, "line_end": 1, "note": "定位"}])

        specs = (SubagentSpec("custom_researcher", "定位自定义实现", locate, timeout_seconds=5.0),)
        result = SubagentCoordinator().run_specs(specs, workspace_root=Path.cwd())

        self.assertEqual("READY", result.status)
        self.assertEqual("SUBAGENTS_COMPLETED", result.code)
        self.assertEqual(["custom_researcher"], [finding.role for finding in result.findings])
        self.assertEqual("READY", result.findings[0].status)
        self.assertEqual("safe", result.findings[0].to_dict()["permission_mode"])

    def test_subagent_timeout_is_recycled_as_blocked(self) -> None:
        def slow(tools: object) -> tuple[str, list[dict[str, object]]]:
            time.sleep(1.0)
            return ("慢子 Agent", [])

        specs = (SubagentSpec("slow", "测试超时", slow, timeout_seconds=0.05),)
        result = SubagentCoordinator().run_specs(specs, workspace_root=Path.cwd())

        self.assertEqual("SUBAGENTS_PARTIAL", result.code)
        self.assertEqual("BLOCKED", result.findings[0].status)
        self.assertIn("超时", result.findings[0].summary)

    def test_invalid_reference_schema_is_blocked(self) -> None:
        def bad_reference(tools: object) -> tuple[str, list[dict[str, object]]]:
            return ("缺 path 的引用", [{"note": "没有 path 字段"}])

        specs = (SubagentSpec("bad", "测试违规产出", bad_reference, timeout_seconds=5.0),)
        result = SubagentCoordinator().run_specs(specs, workspace_root=Path.cwd())

        self.assertEqual("SUBAGENTS_PARTIAL", result.code)
        self.assertEqual("BLOCKED", result.findings[0].status)
        self.assertEqual((), result.findings[0].references)

    def test_invalid_output_shape_is_blocked(self) -> None:
        def bad_shape(tools: object) -> str:
            return "不是 tuple"

        specs = (SubagentSpec("bad_shape", "测试形状错误", bad_shape, timeout_seconds=5.0),)
        result = SubagentCoordinator().run_specs(specs, workspace_root=Path.cwd())

        self.assertEqual("BLOCKED", result.findings[0].status)
        self.assertEqual((), result.findings[0].references)


if __name__ == "__main__":
    unittest.main()
