from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from repopilot_guard.subagents import SubagentCoordinator


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


if __name__ == "__main__":
    unittest.main()
