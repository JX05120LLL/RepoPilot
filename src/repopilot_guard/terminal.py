"""RepoPilot 交互式终端界面，不提供任意 Shell 透传。"""

from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import dataclass
from io import StringIO, TextIOWrapper, UnsupportedOperation
import json
from pathlib import Path
import sys
from typing import Callable, TextIO

from repopilot_guard.permissions import FULL_ACCESS_CONFIRMATION


TERMINAL_HELP = """可用命令：
  welcome                              查看本机就绪状态与推荐下一步
  projects                             列出已注册项目
  tasks                                列出最近任务
  start <项目ID> <safe|full> <change|research> <任务描述>
                                       启动 Coding Agent 任务
  status <线程ID>                      查看任务状态和下一步
  events <线程ID>                      查看脱敏证据事件
  watch <线程ID>                       持续追踪任务事件
  approve <线程ID>                     批准当前审批关卡
  revise <线程ID> <修改意见>           要求重写当前计划
  reject <线程ID>                      拒绝当前审批关卡
  artifacts <线程ID>                   列出任务产物
  artifact <线程ID> <产物类型>         读取经哈希校验的产物
  json <on|off>                        切换原始 JSON 输出，默认 off
  help                                 显示本帮助
  quit                                 退出终端

终端只路由到 RepoPilot 已注册命令，不执行任意 Shell。"""


@dataclass(frozen=True)
class TerminalAction:
    argv: tuple[str, ...] = ()
    message: str | None = None
    exit_requested: bool = False
    confirmation_required: bool = False
    stream_output: bool = False
    output_mode: str | None = None


class TerminalRenderer:
    """将稳定 CLI JSON 契约投影为适合人工阅读的终端摘要。"""

    def render(self, raw: str, argv: list[str], stream: TextIO) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            print(raw.rstrip(), file=stream)
            return
        if not isinstance(payload, dict):
            print(json.dumps(payload, ensure_ascii=False, indent=2), file=stream)
            return
        if payload.get("status") == "BLOCKED":
            self._render_blocked(payload, stream)
            return
        route = tuple(argv[:2])
        if route == ("project", "list"):
            self._render_projects(payload, stream)
        elif route == ("task", "list"):
            self._render_tasks(payload, stream)
        elif route == ("task", "events"):
            self._render_events(payload, stream)
        elif route == ("task", "artifacts"):
            self._render_artifacts(payload, stream)
        elif route == ("task", "artifact"):
            self._render_artifact(payload, stream)
        elif route in {("task", "start"), ("task", "status"), ("task", "decide")}:
            self._render_task(payload, stream)
        elif argv[:1] == ["welcome"]:
            self._render_welcome(payload, stream)
        else:
            self._render_generic(payload, stream)

    @staticmethod
    def _render_projects(payload: dict[str, object], stream: TextIO) -> None:
        projects = payload.get("projects")
        items = projects if isinstance(projects, list) else []
        print(f"项目  {len(items)}", file=stream)
        if not items:
            print("  尚未注册项目。", file=stream)
            return
        for project in items:
            if not isinstance(project, dict):
                continue
            name = _text(project.get("display_name"), "未命名项目")
            project_id = _text(project.get("project_id"), "-")
            kind = "Git" if project.get("is_git_repository") is True else "非 Git"
            print(f"  {name}  [{kind}]", file=stream)
            print(f"    ID  {project_id}", file=stream)

    @staticmethod
    def _render_tasks(payload: dict[str, object], stream: TextIO) -> None:
        tasks = payload.get("tasks")
        items = tasks if isinstance(tasks, list) else []
        print(f"最近任务  {len(items)}", file=stream)
        if not items:
            print("  暂无任务记录。", file=stream)
            return
        for task in items:
            if not isinstance(task, dict):
                continue
            status = _text(task.get("status"), "UNKNOWN")
            title = _truncate(_text(task.get("display_title"), "未命名任务"), 72)
            print(f"  [{status}] {title}", file=stream)
            print(f"    线程  {_text(task.get('thread_id'), '-')}", file=stream)
            print(
                "    模式  "
                f"{_text(task.get('task_mode'), '-')} / {_text(task.get('task_operation'), '-')}",
                file=stream,
            )

    @staticmethod
    def _render_events(payload: dict[str, object], stream: TextIO) -> None:
        events = payload.get("events")
        items = events if isinstance(events, list) else []
        print(
            f"证据事件  {len(items)}  下一游标 {_text(payload.get('next_sequence'), '0')}",
            file=stream,
        )
        for event in items:
            if not isinstance(event, dict):
                continue
            sequence = _text(event.get("sequence"), "-")
            event_type = _text(event.get("event_type", event.get("type")), "EVIDENCE")
            event_payload = event.get("payload")
            summary = ""
            if isinstance(event_payload, dict):
                summary = _text(
                    event_payload.get("message", event_payload.get("code", event_payload.get("status"))),
                    "",
                )
            suffix = f"  {_truncate(summary, 88)}" if summary else ""
            print(f"  {sequence:>4}  {event_type}{suffix}", file=stream)

    @staticmethod
    def _render_artifacts(payload: dict[str, object], stream: TextIO) -> None:
        artifacts = payload.get("artifacts")
        items = artifacts if isinstance(artifacts, list) else []
        print(f"任务产物  {len(items)}", file=stream)
        for artifact in items:
            if not isinstance(artifact, dict):
                continue
            kind = _text(artifact.get("kind"), "unknown")
            size = _text(artifact.get("size_bytes"), "0")
            digest = _text(artifact.get("sha256"), "-")[:12]
            print(f"  {kind:<18} {size:>8} B  sha256:{digest}", file=stream)

    @staticmethod
    def _render_artifact(payload: dict[str, object], stream: TextIO) -> None:
        artifact = payload.get("artifact")
        metadata = artifact if isinstance(artifact, dict) else {}
        print(
            "产物  "
            f"{_text(metadata.get('kind'), 'unknown')}  "
            f"sha256:{_text(metadata.get('sha256'), '-')[:12]}",
            file=stream,
        )
        print("-" * 72, file=stream)
        content = payload.get("content")
        print(content if isinstance(content, str) else "产物正文不可用。", file=stream)

    @staticmethod
    def _render_task(payload: dict[str, object], stream: TextIO) -> None:
        status = _text(payload.get("status"), "UNKNOWN")
        verdict = _text(payload.get("verdict"), "-")
        print(f"任务  [{status}]  验证 {verdict}", file=stream)
        title = payload.get("display_title")
        if isinstance(title, str) and title.strip():
            print(f"  {_truncate(title, 96)}", file=stream)
        print(f"  线程  {_text(payload.get('thread_id'), '-')}", file=stream)
        progress = payload.get("progress")
        if isinstance(progress, dict):
            summary = progress.get("summary")
            if isinstance(summary, str) and summary.strip():
                print(f"  进度  {summary}", file=stream)
        approval = payload.get("approval")
        if isinstance(approval, dict):
            stage = _text(approval.get("stage", approval.get("type")), "待审批")
            print(f"  审批  {stage}", file=stream)
            files = approval.get("candidate_files")
            if isinstance(files, list) and files:
                print("  文件  " + "、".join(str(item) for item in files[:5]), file=stream)
        TerminalRenderer._render_next_action(payload, stream)

    @staticmethod
    def _render_welcome(payload: dict[str, object], stream: TextIO) -> None:
        print("RepoPilot 本机状态", file=stream)
        summary = payload.get("summary")
        if isinstance(summary, str):
            print(f"  {summary}", file=stream)
        projects = payload.get("projects")
        if isinstance(projects, list):
            print(f"  已注册项目  {len(projects)}", file=stream)
        TerminalRenderer._render_next_action(payload, stream)

    @staticmethod
    def _render_blocked(payload: dict[str, object], stream: TextIO) -> None:
        print(f"BLOCKED  {_text(payload.get('code'), 'UNKNOWN')}", file=stream)
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            print(f"  {message}", file=stream)
        TerminalRenderer._render_next_action(payload, stream)

    @staticmethod
    def _render_generic(payload: dict[str, object], stream: TextIO) -> None:
        status = _text(payload.get("status"), "READY")
        code = _text(payload.get("code"), "")
        print(f"{status}{'  ' + code if code else ''}", file=stream)
        message = payload.get("message")
        if isinstance(message, str):
            print(f"  {message}", file=stream)
        TerminalRenderer._render_next_action(payload, stream)

    @staticmethod
    def _render_next_action(payload: dict[str, object], stream: TextIO) -> None:
        next_action = payload.get("next_action")
        if not isinstance(next_action, dict):
            return
        command = next_action.get("command")
        if isinstance(command, str) and command.strip():
            print(f"  下一步  {command}", file=stream)


def _text(value: object, fallback: str) -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text or fallback


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


class TerminalCommandRouter:
    """把有限的交互命令映射到现有 CLI 契约。"""

    def route(self, raw: str) -> TerminalAction:
        line = raw.strip()
        if not line:
            return TerminalAction()
        command = line.split(maxsplit=1)[0].lower()
        if command in {"quit", "exit", "q"}:
            return TerminalAction(exit_requested=True)
        if command in {"help", "?"}:
            return TerminalAction(message=TERMINAL_HELP)
        if command == "json":
            parts = line.split()
            if len(parts) != 2 or parts[1].lower() not in {"on", "off"}:
                return self._usage("用法：json <on|off>")
            mode = "json" if parts[1].lower() == "on" else "human"
            return TerminalAction(output_mode=mode)
        if command == "welcome":
            return TerminalAction(argv=("welcome",))
        if command == "projects":
            return TerminalAction(argv=("project", "list"))
        if command == "tasks":
            return TerminalAction(argv=("task", "list"))
        if command == "start":
            return self._route_start(line)
        if command == "revise":
            parts = line.split(maxsplit=2)
            if len(parts) != 3 or not parts[2].strip():
                return self._usage("用法：revise <线程ID> <修改意见>")
            return TerminalAction(
                argv=(
                    "task",
                    "decide",
                    "--thread-id",
                    parts[1],
                    "--decision",
                    "revise",
                    "--comment",
                    parts[2].strip(),
                )
            )
        if command == "artifact":
            parts = line.split(maxsplit=2)
            if len(parts) != 3:
                return self._usage("用法：artifact <线程ID> <产物类型>")
            return TerminalAction(
                argv=(
                    "task",
                    "artifact",
                    "--thread-id",
                    parts[1],
                    "--kind",
                    parts[2],
                )
            )
        single_argument_commands = {
            "status": "status",
            "events": "events",
            "watch": "watch",
            "artifacts": "artifacts",
        }
        if command in single_argument_commands:
            action = self._route_task_argument(
                line,
                command,
                single_argument_commands[command],
            )
            if command == "watch" and action.argv:
                return TerminalAction(
                    argv=action.argv,
                    message="实时追踪使用 JSONL 输出；按 Ctrl+C 可中断并保留游标。",
                    stream_output=True,
                )
            return action
        decisions = {"approve": "approve", "reject": "reject"}
        if command in decisions:
            parts = line.split()
            if len(parts) != 2:
                return self._usage(f"用法：{command} <线程ID>")
            return TerminalAction(
                argv=(
                    "task",
                    "decide",
                    "--thread-id",
                    parts[1],
                    "--decision",
                    decisions[command],
                )
            )
        return self._usage("未知命令。输入 help 查看受支持的命令。")

    def _route_start(self, line: str) -> TerminalAction:
        parts = line.split(maxsplit=4)
        if len(parts) != 5:
            return self._usage(
                "用法：start <项目ID> <safe|full> <change|research> <任务描述>"
            )
        _, project_id, mode, operation, description = parts
        if mode not in {"safe", "full"}:
            return self._usage("任务模式必须是 safe 或 full。")
        if operation not in {"change", "research"}:
            return self._usage("任务类型必须是 change 或 research。")
        task_mode = "safe-isolated" if mode == "safe" else "full-local"
        return TerminalAction(
            argv=(
                "task",
                "start",
                "--project-id",
                project_id,
                "--task-mode",
                task_mode,
                "--operation",
                operation,
                "--task",
                description.strip(),
            ),
            confirmation_required=mode == "full",
        )

    @staticmethod
    def _route_task_argument(
        line: str,
        command: str,
        task_subcommand: str,
    ) -> TerminalAction:
        parts = line.split()
        if len(parts) != 2:
            return TerminalCommandRouter._usage(f"用法：{command} <线程ID>")
        return TerminalAction(
            argv=("task", task_subcommand, "--thread-id", parts[1])
        )

    @staticmethod
    def _usage(message: str) -> TerminalAction:
        return TerminalAction(message=message)


def run_terminal(
    execute: Callable[[list[str]], int],
    *,
    state_db: Path | None = None,
    input_func: Callable[[str], str] | None = None,
    output: TextIO | None = None,
) -> int:
    """运行受控 REPL；所有副作用仍由现有 CLI、PolicyGuard 和 Graph 执行。"""

    read = input_func or input
    if output is None and isinstance(sys.stdout, TextIOWrapper):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except UnsupportedOperation:
            pass
    stream = output or sys.stdout
    router = TerminalCommandRouter()
    renderer = TerminalRenderer()
    raw_json = False
    print("RepoPilot Terminal", file=stream)
    print("本地 Coding Agent 会话。输入 help 查看命令，输入 quit 退出。", file=stream)
    while True:
        try:
            raw = read("repopilot> ")
        except EOFError:
            print("\n终端输入已关闭。", file=stream)
            return 0
        except KeyboardInterrupt:
            print("\n已退出 RepoPilot Terminal。", file=stream)
            return 130
        action = router.route(raw)
        if action.exit_requested:
            print("已退出 RepoPilot Terminal。", file=stream)
            return 0
        if action.output_mode is not None:
            raw_json = action.output_mode == "json"
            print(
                "已切换为原始 JSON 输出。" if raw_json else "已切换为人类可读输出。",
                file=stream,
            )
            continue
        if action.message:
            print(action.message, file=stream)
        if not action.argv:
            continue
        argv = list(action.argv)
        if action.confirmation_required:
            confirmation = read(
                f"完全本机控制将直接操作当前项目。请输入确认语句：{FULL_ACCESS_CONFIRMATION}\n确认> "
            )
            if confirmation != FULL_ACCESS_CONFIRMATION:
                print("BLOCKED：确认语句不匹配，任务未启动。", file=stream)
                continue
            argv.extend(("--confirm-full-access", confirmation))
        if state_db is not None:
            argv.extend(("--state-db", str(state_db)))
        if raw_json or action.stream_output:
            with redirect_stdout(stream):
                exit_code = execute(argv)
        else:
            captured = StringIO()
            with redirect_stdout(captured):
                exit_code = execute(argv)
            rendered = captured.getvalue().strip()
            if rendered:
                renderer.render(rendered, argv, stream)
        if exit_code != 0:
            print(f"命令返回非零状态：{exit_code}", file=stream)
