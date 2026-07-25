from __future__ import annotations

from io import StringIO
import json
from pathlib import Path
import unittest

from repopilot_guard.permissions import FULL_ACCESS_CONFIRMATION
from repopilot_guard.terminal import TerminalCommandRouter, TerminalRenderer, run_terminal


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


if __name__ == "__main__":
    unittest.main()
