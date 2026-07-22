"""为每个 Agent 任务保存仅追加、已脱敏的证据产物。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from repopilot_guard.models import TaskResult


_SENSITIVE_KEYS = frozenset({"api_key", "token", "password", "secret", "credential", "authorization"})


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
