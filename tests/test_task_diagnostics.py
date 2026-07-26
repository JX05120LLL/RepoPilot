from __future__ import annotations

import unittest

from repopilot_guard.task_diagnostics import build_task_diagnostic


class TaskDiagnosticsTests(unittest.TestCase):
    def test_preflight_block_uses_safe_runtime_recovery(self) -> None:
        diagnostic = build_task_diagnostic(
            status="BLOCKED",
            verdict="BLOCKED",
            error_summary="预检未通过，任务已阻断。",
        )

        self.assertEqual("PREFLIGHT_BLOCKED", diagnostic["code"])
        self.assertEqual("OPEN_RUNTIME_CONFIGURATION", diagnostic["recommended_action"])
        self.assertEqual("danger", diagnostic["tone"])

    def test_git_runtime_failure_does_not_expose_raw_exception_text(self) -> None:
        diagnostic = build_task_diagnostic(
            status="BLOCKED",
            error_summary="TASK_RUNTIME_FAILED: GitCommandError password=should-never-appear",
        )

        self.assertEqual("GIT_WORKSPACE_CHECK_REQUIRED", diagnostic["code"])
        rendered = " ".join(diagnostic.values())
        self.assertNotIn("password", rendered)
        self.assertNotIn("GitCommandError", rendered)

    def test_failed_task_points_to_verification_evidence(self) -> None:
        diagnostic = build_task_diagnostic(status="FAILED", verdict="FAILED")

        self.assertEqual("PATCH_OR_VERIFICATION_FAILED", diagnostic["code"])
        self.assertEqual("OPEN_VERIFICATION", diagnostic["recommended_action"])

    def test_repository_preflight_event_takes_priority_over_generic_block(self) -> None:
        diagnostic = build_task_diagnostic(
            status="BLOCKED",
            error_summary="预检未通过，任务已阻断。",
            evidence_codes=[
                {
                    "type": "PREFLIGHT_COMPLETED",
                    "checks": [
                        {"status": "BLOCKED", "code": "REPOSITORY_PREFLIGHT_FAILED"},
                    ],
                }
            ],
        )

        self.assertEqual("PROJECT_PREFLIGHT_REQUIRED", diagnostic["code"])
        self.assertEqual("OPEN_TASK_EVIDENCE", diagnostic["recommended_action"])

    def test_preextracted_evidence_codes_are_preserved(self) -> None:
        diagnostic = build_task_diagnostic(
            status="BLOCKED",
            evidence_codes={"REPOSITORY_PREFLIGHT_FAILED"},
        )

        self.assertEqual("PROJECT_PREFLIGHT_REQUIRED", diagnostic["code"])

    def test_pending_approval_is_not_reported_as_a_failure(self) -> None:
        diagnostic = build_task_diagnostic(
            status="WAITING_APPROVAL",
            pending_approval=True,
        )

        self.assertEqual("PENDING_APPROVAL", diagnostic["code"])
        self.assertEqual("warning", diagnostic["tone"])


if __name__ == "__main__":
    unittest.main()
