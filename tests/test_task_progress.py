from __future__ import annotations

import unittest

from repopilot_guard.task_progress import build_task_progress


class TaskProgressTests(unittest.TestCase):
    def test_plan_approval_is_the_current_stage(self) -> None:
        progress = build_task_progress(
            status="WAITING_APPROVAL",
            pending_approval=True,
            pending_approval_action="PLAN_REVIEW",
            task_operation="change",
            tool_events=[{"type": "NODE_COMPLETED", "node": "PLAN"}],
        )

        self.assertEqual("plan_approval", progress["current_stage"])
        self.assertFalse(progress["terminal"])
        self.assertEqual("current", _stage_state(progress, "plan_approval"))
        self.assertEqual("pending", _stage_state(progress, "execution_approval"))

    def test_execution_approval_does_not_claim_patch_started(self) -> None:
        progress = build_task_progress(
            status="WAITING_APPROVAL",
            pending_approval=True,
            pending_approval_action="EXECUTION_REVIEW",
            task_operation="change",
        )

        self.assertEqual("execution_approval", progress["current_stage"])
        self.assertEqual("current", _stage_state(progress, "execution_approval"))
        self.assertEqual("pending", _stage_state(progress, "patch"))

    def test_maven_failure_is_marked_as_failed_without_passing_report(self) -> None:
        progress = build_task_progress(
            status="REPORT",
            verdict="FAILED",
            task_operation="change",
            verification={"status": "FAILED", "code": "MAVEN_TEST_FAILED"},
        )

        self.assertTrue(progress["terminal"])
        self.assertEqual("failed", progress["terminal_kind"])
        self.assertEqual("verify", progress["current_stage"])
        self.assertEqual("failed", _stage_state(progress, "verify"))
        self.assertEqual("pending", _stage_state(progress, "report"))

    def test_blocked_task_uses_last_executed_node_without_claiming_success(self) -> None:
        progress = build_task_progress(
            status="BLOCKED",
            verdict="BLOCKED",
            task_operation="change",
            tool_events=[
                {"type": "NODE_COMPLETED", "node": "WORKSPACE"},
                {"type": "NODE_COMPLETED", "node": "PREFLIGHT"},
            ],
        )

        self.assertEqual("preflight", progress["current_stage"])
        self.assertEqual("blocked", _stage_state(progress, "preflight"))
        self.assertEqual("pending", _stage_state(progress, "context"))

    def test_passed_task_requires_report_evidence_and_marks_all_stages_complete(self) -> None:
        progress = build_task_progress(
            status="REPORT",
            verdict="PASSED",
            task_operation="change",
            verification={"status": "PASSED"},
        )

        self.assertEqual("report", progress["current_stage"])
        self.assertEqual("passed", _stage_state(progress, "report"))
        self.assertEqual("completed", _stage_state(progress, "verify"))

    def test_research_report_skips_write_and_maven_stages(self) -> None:
        progress = build_task_progress(status="REPORT", verdict="UNVERIFIED", task_operation="research")

        self.assertEqual("unverified", progress["terminal_kind"])
        self.assertEqual(
            ["workspace", "preflight", "context", "research", "plan_approval", "report"],
            [stage["id"] for stage in progress["stages"]],
        )


def _stage_state(progress: dict[str, object], stage_id: str) -> str:
    stages = progress["stages"]
    assert isinstance(stages, list)
    for stage in stages:
        if isinstance(stage, dict) and stage.get("id") == stage_id:
            state = stage.get("state")
            assert isinstance(state, str)
            return state
    raise AssertionError(f"stage not found: {stage_id}")
