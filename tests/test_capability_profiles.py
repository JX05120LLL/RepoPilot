from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from repopilot_guard.project_registry import ProjectRegistry
from repopilot_guard.evaluation import _tool_trajectory_matches


class CapabilityProfileTests(unittest.TestCase):
    def test_tool_trajectory_uses_ordered_subsequence_for_stable_regressions(self) -> None:
        self.assertTrue(_tool_trajectory_matches(("list_files", "search_code", "read_file"), ("search_code", "read_file")))
        self.assertFalse(_tool_trajectory_matches(("read_file", "search_code"), ("search_code", "read_file")))
    def test_registered_project_generates_confirmable_profile_without_source_body(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "demo"
            controller = repository / "src/main/java/demo/OrderController.java"
            controller.parent.mkdir(parents=True)
            (repository / "pom.xml").write_text("<project><modules><module>service</module></modules></project>", encoding="utf-8")
            (repository / "service").mkdir()
            controller.write_text("class OrderController { String secret = \"not persisted\"; }", encoding="utf-8")
            registry = ProjectRegistry(root / "state.sqlite")
            try:
                project = registry.add(repository)
                profile = registry.capability_profile(project.project_id)
                self.assertEqual("PENDING_CONFIRMATION", profile.status)
                self.assertEqual("http_controller", profile.facts["entrypoints"][0]["kind"])
                self.assertNotIn("not persisted", str(profile.to_dict()))

                confirmed = registry.confirm_capability_profile(
                    project.project_id,
                    profile.profile_sha256,
                    ("订单状态不可跳过支付成功",),
                    ("src/main/resources/application-prod.yml",),
                )
                self.assertEqual("CONFIRMED", confirmed.status)
                self.assertIn("订单状态不可跳过支付成功", confirmed.context_payload()["business_rules"])

                (repository / "src/main/java/demo/PaymentController.java").write_text("class PaymentController {}", encoding="utf-8")
                refreshed = registry.capability_profile(project.project_id)
                self.assertEqual("PENDING_CONFIRMATION", refreshed.status)
                self.assertNotEqual(profile.profile_sha256, refreshed.profile_sha256)
            finally:
                registry.close()
