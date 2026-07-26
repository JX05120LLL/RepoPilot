from __future__ import annotations

from io import StringIO
import json
from pathlib import Path
import unittest

from repopilot_guard.permissions import FULL_ACCESS_CONFIRMATION
from repopilot_guard.terminal import (
    TerminalCommandRouter,
    TerminalRenderer,
    TerminalSessionContext,
    run_terminal,
)


class TerminalCommandRouterTests(unittest.TestCase):
    def test_router_maps_whitelisted_task_commands_without_shell_passthrough(self) -> None:
        router = TerminalCommandRouter()

        action = router.route("start project-1 safe research 介绍这个项目的架构")

        self.assertEqual(action.argv[:2], ("task", "start"))
        self.assertIn("safe-isolated", action.argv)
        self.assertIn("介绍这个项目的架构", action.argv)
        self.assertFalse(action.confirmation_required)
        unknown = router.route("powershell Remove-Item -Recurse .")
        self.assertEqual(unknown.argv, ())
        self.assertIn("未知命令", unknown.message or "")

    def test_full_local_requires_exact_confirmation_before_execution(self) -> None:
        commands: list[list[str]] = []
        answers = iter(
            (
                "start project-1 full research 分析项目",
                "错误确认",
                "start project-1 full research 分析项目",
                FULL_ACCESS_CONFIRMATION,
                "quit",
            )
        )
        output = StringIO()

        exit_code = run_terminal(
            lambda argv: commands.append(argv) or 0,
            state_db=Path("state.sqlite"),
            input_func=lambda _prompt: next(answers),
            output=output,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(commands), 1)
        self.assertIn("--confirm-full-access", commands[0])
        self.assertEqual(commands[0][-2:], ["--state-db", "state.sqlite"])
        self.assertIn("BLOCKED", output.getvalue())

    def test_terminal_routes_status_and_ends_cleanly(self) -> None:
        commands: list[list[str]] = []
        answers = iter(("status thread-1", "quit"))

        exit_code = run_terminal(
            lambda argv: commands.append(argv) or 0,
            input_func=lambda _prompt: next(answers),
            output=StringIO(),
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            commands,
            [["task", "status", "--thread-id", "thread-1"]],
        )

    def test_router_uses_current_project_and_task_without_relaxing_cli_contract(self) -> None:
        router = TerminalCommandRouter()
        session = TerminalSessionContext(
            project_id="project-1",
            thread_id="thread-1",
        )

        start = router.route("start safe research 分析当前项目", session)
        status = router.route("status", session)
        review = router.route("review", session)
        artifact = router.route("artifact report", session)

        self.assertIn("project-1", start.argv)
        self.assertIn("safe-isolated", start.argv)
        self.assertEqual(
            status.argv,
            ("task", "status", "--thread-id", "thread-1"),
        )
        self.assertEqual(
            review.argv,
            ("task", "review", "--thread-id", "thread-1"),
        )
        self.assertEqual(
            artifact.argv,
            (
                "task",
                "artifact",
                "--thread-id",
                "thread-1",
                "--kind",
                "report",
            ),
        )

    def test_terminal_validates_context_and_remembers_started_task(self) -> None:
        commands: list[list[str]] = []
        answers = iter(
            (
                "use project project-1",
                "start safe research 分析当前项目",
                "current",
                "status",
                "quit",
            )
        )
        output = StringIO()

        def execute(argv: list[str]) -> int:
            commands.append(argv)
            if argv[:2] == ["project", "doctor"]:
                print('{"status":"READY","code":"PROJECT_READY"}')
            elif argv[:2] == ["task", "start"]:
                print(
                    '{"status":"WAITING_APPROVAL","verdict":"UNVERIFIED",'
                    '"thread_id":"thread-2","display_title":"分析当前项目"}'
                )
            else:
                print(
                    '{"status":"WAITING_APPROVAL","verdict":"UNVERIFIED",'
                    '"thread_id":"thread-2"}'
                )
            return 0

        run_terminal(
            execute,
            input_func=lambda _prompt: next(answers),
            output=output,
        )

        self.assertEqual(commands[0][:4], ["project", "doctor", "--project-id", "project-1"])
        self.assertIn("project-1", commands[1])
        self.assertEqual(commands[2], ["task", "status", "--thread-id", "thread-2"])
        self.assertIn("项目  project-1", output.getvalue())
        self.assertIn("任务  thread-2", output.getvalue())

    def test_failed_context_validation_does_not_change_session(self) -> None:
        answers = iter(("use project missing", "current", "quit"))
        output = StringIO()

        def execute(_argv: list[str]) -> int:
            print('{"status":"BLOCKED","code":"PROJECT_NOT_FOUND"}')
            return 2

        run_terminal(
            execute,
            input_func=lambda _prompt: next(answers),
            output=output,
        )

        self.assertIn("项目  未选择", output.getvalue())
        self.assertNotIn("项目  missing", output.getvalue())

    def test_terminal_renders_project_json_as_human_readable_summary(self) -> None:
        answers = iter(("projects", "quit"))
        output = StringIO()

        def execute(_argv: list[str]) -> int:
            print(json.dumps({
                "status": "READY",
                "projects": [{
                    "project_id": "project-1",
                    "display_name": "订单服务",
                    "root_path": "D:/secret/repository",
                    "is_git_repository": True,
                }],
            }, ensure_ascii=False))
            return 0

        run_terminal(
            execute,
            input_func=lambda _prompt: next(answers),
            output=output,
        )

        rendered = output.getvalue()
        self.assertIn("项目  1", rendered)
        self.assertIn("订单服务  [Git]", rendered)
        self.assertNotIn("D:/secret/repository", rendered)
        self.assertNotIn('"projects"', rendered)

    def test_json_mode_preserves_machine_readable_cli_output(self) -> None:
        answers = iter(("json on", "tasks", "quit"))
        output = StringIO()

        def execute(_argv: list[str]) -> int:
            print('{"status":"READY","tasks":[]}')
            return 0

        run_terminal(
            execute,
            input_func=lambda _prompt: next(answers),
            output=output,
        )

        self.assertIn("已切换为原始 JSON 输出", output.getvalue())
        self.assertIn('{"status":"READY","tasks":[]}', output.getvalue())

    def test_json_mode_still_remembers_started_thread(self) -> None:
        answers = iter(
            (
                "use project project-1",
                "json on",
                "start safe research 分析项目",
                "json off",
                "current",
                "quit",
            )
        )
        output = StringIO()

        def execute(argv: list[str]) -> int:
            if argv[:2] == ["project", "doctor"]:
                print('{"status":"READY"}')
            else:
                print('{"status":"WAITING_APPROVAL","thread_id":"thread-json"}')
            return 0

        run_terminal(
            execute,
            input_func=lambda _prompt: next(answers),
            output=output,
        )

        self.assertIn("任务  thread-json", output.getvalue())

    def test_watch_keeps_streaming_jsonl_contract(self) -> None:
        router = TerminalCommandRouter()

        action = router.route("watch thread-1")

        self.assertTrue(action.stream_output)
        self.assertEqual(action.argv[:2], ("task", "watch"))
        self.assertIn("JSONL", action.message or "")

    def test_renderer_surfaces_blocked_code_without_raw_payload(self) -> None:
        output = StringIO()

        TerminalRenderer().render(
            json.dumps({
                "status": "BLOCKED",
                "code": "QDRANT_UNAVAILABLE",
                "message": "Qdrant 未就绪。",
                "secret": "must-not-render",
            }, ensure_ascii=False),
            ["task", "status"],
            output,
        )

        self.assertIn("BLOCKED  QDRANT_UNAVAILABLE", output.getvalue())
        self.assertIn("Qdrant 未就绪", output.getvalue())
        self.assertNotIn("must-not-render", output.getvalue())

    def test_renderer_summarizes_review_without_rendering_event_secrets(self) -> None:
        output = StringIO()

        TerminalRenderer().render(
            json.dumps(
                {
                    "status": "READY",
                    "task": {"status": "WAITING_APPROVAL", "verdict": "UNVERIFIED", "display_title": "修复订单校验"},
                    "recent_events": [{"sequence": 7, "type": "PLAN_READY", "payload": {"code": "PLAN_READY", "secret": "must-not-render"}}],
                    "artifacts": [{"kind": "plan_markdown", "sha256": "a" * 64}],
                },
                ensure_ascii=False,
            ),
            ["task", "review"],
            output,
        )

        rendered = output.getvalue()
        self.assertIn("任务审阅", rendered)
        self.assertIn("PLAN_READY", rendered)
        self.assertIn("修改计划", rendered)
        self.assertNotIn("must-not-render", rendered)

    def test_renderer_review_surfaces_only_the_safe_artifact_next_step(self) -> None:
        output = StringIO()

        TerminalRenderer().render(
            json.dumps(
                {
                    "status": "READY",
                    "task": {"status": "REPORT", "verdict": "FAILED"},
                    "recent_events": [],
                    "artifacts": [{"kind": "verification", "sha256": "b" * 64}],
                    "next_action": {
                        "type": "READ_VERIFICATION_EVIDENCE",
                        "command": "repopilot-guard task artifact --thread-id thread-1 --kind verification",
                    },
                },
                ensure_ascii=False,
            ),
            ["task", "review"],
            output,
        )

        rendered = output.getvalue()
        self.assertIn("任务审阅  已生成报告 · 验证未通过", rendered)
        self.assertIn("验证结果", rendered)
        self.assertIn("核验 Maven 验证记录", rendered)
        self.assertIn("task artifact --thread-id thread-1 --kind verification", rendered)
        self.assertNotIn("[REPORT]", rendered)


if __name__ == "__main__":
    unittest.main()
