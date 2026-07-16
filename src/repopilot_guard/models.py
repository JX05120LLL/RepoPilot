"""Agent 控制平面共享的领域模型。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from tempfile import gettempdir
from typing import Any
from uuid import uuid4


def default_output_root() -> Path:
    """将运行产物放在被检视仓库之外，避免污染原始工作区。"""
    return Path(gettempdir()) / "repopilot-guard" / "runs"


class TaskState(str, Enum):
    PREFLIGHT = "PREFLIGHT"
    UNDERSTAND = "UNDERSTAND"
    LOCATE = "LOCATE"
    PLAN = "PLAN"
    PATCH = "PATCH"
    TEST = "TEST"
    REPAIR = "REPAIR"
    REVIEW = "REVIEW"
    REPORT = "REPORT"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class TaskVerdict(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    PARTIAL = "PARTIAL"
    UNVERIFIED = "UNVERIFIED"


@dataclass(frozen=True, slots=True)
class TaskRequest:
    repository: Path
    description: str
    output_root: Path = field(default_factory=default_output_root)
    task_id: str = field(default_factory=lambda: f"task-{uuid4().hex[:12]}")
    max_steps: int = 12

    def __post_init__(self) -> None:
        if not self.description.strip():
            raise ValueError("Task description must not be blank.")
        if self.max_steps < 1:
            raise ValueError("max_steps must be at least 1.")

        repository = self.repository.expanduser().resolve()
        output_root = self.output_root.expanduser().resolve()
        try:
            output_root.relative_to(repository)
        except ValueError:
            pass
        else:
            raise ValueError("output_root must be outside the inspected repository.")

        object.__setattr__(self, "repository", repository)
        object.__setattr__(self, "output_root", output_root)


@dataclass(frozen=True, slots=True)
class PreflightResult:
    repository: Path
    is_git_repository: bool
    has_pom_xml: bool
    java_source_root: Path | None
    maven_wrapper: Path | None
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository": str(self.repository),
            "is_git_repository": self.is_git_repository,
            "has_pom_xml": self.has_pom_xml,
            "java_source_root": str(self.java_source_root) if self.java_source_root else None,
            "maven_wrapper": str(self.maven_wrapper) if self.maven_wrapper else None,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class TaskResult:
    task_id: str
    repository: Path
    verdict: TaskVerdict
    final_state: TaskState
    state_history: tuple[TaskState, ...]
    preflight: PreflightResult
    report_path: Path
    events_path: Path
    message: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["repository"] = str(self.repository)
        payload["verdict"] = self.verdict.value
        payload["final_state"] = self.final_state.value
        payload["state_history"] = [state.value for state in self.state_history]
        payload["preflight"] = self.preflight.to_dict()
        payload["report_path"] = str(self.report_path)
        payload["events_path"] = str(self.events_path)
        payload["created_at"] = self.created_at.isoformat()
        return payload
