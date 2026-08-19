"""为每个 Agent 任务保存仅追加、已脱敏的证据产物。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from repopilot_guard.models import TaskResult


_SENSITIVE_KEYS = frozenset({"api_key", "token", "password", "secret", "credential", "authorization"})

# 任务证据流的终态事件：只有这些事件作为最后一条时，任务才算「完整收尾」。
# 崩溃补齐（repair_interrupted）只对这些之外的尾部追加 INTERRUPTED，不修改已有记录。
_TERMINAL_EVENT_TYPES = frozenset(
    {"task_completed", "task_blocked", "workspace_prepared", "workspace_blocked", "INTERRUPTED"}
)


def _redact(value: Any, key: str | None = None) -> Any:
    if key and key.lower() in _SENSITIVE_KEYS:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {name: _redact(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


class EvidenceStore:
    """在被检视仓库外保存事件，以保护原始源码完整性。"""

    def __init__(self, output_root: Path, task_id: str) -> None:
        self.run_directory = output_root / task_id
        # worktree 与证据都属于同一个任务产物目录，恢复任务时只追加事件。
        self.run_directory.mkdir(parents=True, exist_ok=True)
        self.events_path = self.run_directory / "events.jsonl"
        self.report_path = self.run_directory / "report.md"

    def record(self, event_type: str, payload: dict[str, Any]) -> None:
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "payload": _redact(payload),
        }
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True))
            handle.write("\n")

    def _last_event_type(self) -> str | None:
        """读取事件流最后一条非空记录的 event_type；文件缺失或损坏返回 None。"""

        if not self.events_path.is_file():
            return None
        last_line: str | None = None
        with self.events_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped:
                    last_line = stripped
        if last_line is None:
            return None
        try:
            return json.loads(last_line).get("event_type")
        except (json.JSONDecodeError, AttributeError):
            return None

    def repair_interrupted(self) -> bool:
        """为崩溃中断、尾部无终态事件的任务补写 INTERRUPTED 终态（只追加，不改已有记录）。

        返回是否追加：仅在事件流已有内容、且最后一条不是终态事件时才补一句。
        缺失或空的事件文件不产生任何事件——没有证据可补齐。
        """

        if not self.events_path.is_file():
            return False
        last = self._last_event_type()
        if last is None or last in _TERMINAL_EVENT_TYPES:
            return False
        self.record("INTERRUPTED", {"reason": "task_interrupted"})
        return True

    def write_report(self, result: TaskResult) -> Path:
        preflight = result.preflight
        lines = [
            "# RepoPilot Guard Run Report",
            "",
            "## Task",
            "",
            f"- Task ID: `{result.task_id}`",
            f"- Repository: `{result.repository}`",
            f"- Verdict: `{result.verdict.value}`",
            f"- Final State: `{result.final_state.value}`",
            f"- Message: {result.message}",
            "",
            "## State History",
            "",
            " -> ".join(state.value for state in result.state_history),
            "",
            "## Preflight Evidence",
            "",
            f"- Git working tree: `{preflight.is_git_repository}`",
            f"- Maven pom.xml: `{preflight.has_pom_xml}`",
            f"- Java source root: `{preflight.java_source_root or 'not found'}`",
            f"- Maven Wrapper: `{preflight.maven_wrapper or 'not found'}`",
            "",
            "## Warnings",
            "",
        ]
        lines.extend([f"- {warning}" for warning in preflight.warnings] or ["- None"])
        lines.extend(["", "## Blocking Errors", ""])
        lines.extend([f"- {error}" for error in preflight.errors] or ["- None"])
        lines.extend(
            [
                "",
                "## Verification",
                "",
                "No code patch or Maven command was executed in this skeleton stage.",
                "The run is therefore never reported as PASSED.",
                "",
                f"Raw event evidence: `{self.events_path.name}`",
            ]
        )
        self.report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return self.report_path


def repair_interrupted_evidence(output_root: Path) -> tuple[str, ...]:
    """扫描 output_root 下所有任务目录，为尾部无终态事件的证据流补写 INTERRUPTED。

    只补不改：对每个任务目录实例化 EvidenceStore 后调用 repair_interrupted，
    缺失/空/已完整收尾的事件文件均不受影响。返回已补齐的任务 id。
    """

    root = output_root.expanduser()
    if not root.is_dir():
        return ()
    repaired: list[str] = []
    for task_dir in sorted(root.iterdir()):
        if not task_dir.is_dir():
            continue
        store = EvidenceStore(root, task_dir.name)
        if store.repair_interrupted():
            repaired.append(task_dir.name)
    return tuple(repaired)
