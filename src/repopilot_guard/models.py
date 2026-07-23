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


class WorkspaceMode(str, Enum):
    """任务实际执行位置。"""

    LOCAL = "local"
    WORKTREE = "worktree"


class TaskMode(str, Enum):
    """桌面端暴露的两种固定产品模式，避免用户拼装危险组合。"""

    SAFE_ISOLATED = "safe-isolated"
    FULL_LOCAL = "full-local"

    @property
    def workspace_mode(self) -> WorkspaceMode:
        return WorkspaceMode.WORKTREE if self is TaskMode.SAFE_ISOLATED else WorkspaceMode.LOCAL

    @property
    def permission_mode(self) -> str:
        return "safe" if self is TaskMode.SAFE_ISOLATED else "full"


class TaskOperation(str, Enum):
    """任务的控制面目标；模型不能把只读研究升级为代码执行。"""

    CHANGE = "change"
    RESEARCH = "research"


@dataclass(frozen=True, slots=True)
class WorkspaceSelection:
    """用户为任务选择的 Git 基线与工作目录策略。"""

    mode: WorkspaceMode = WorkspaceMode.WORKTREE
    start_ref: str = "HEAD"
    include_uncommitted_changes: bool = False

    def __post_init__(self) -> None:
        if not self.start_ref.strip():
            raise ValueError("start_ref must not be blank.")


@dataclass(frozen=True, slots=True)
class VerificationContract:
    """由控制面指定的验证约束，模型只能遵守，不能自行改写。"""

    recipe: str
    target_test_class: str | None = None

    def __post_init__(self) -> None:
        if self.recipe not in {"compile", "test", "targeted_test"}:
            raise ValueError("verification recipe is not allowed.")
        if self.recipe == "targeted_test" and not self.target_test_class:
            raise ValueError("targeted_test requires target_test_class.")
        if self.recipe != "targeted_test" and self.target_test_class is not None:
            raise ValueError("compile/test must not define target_test_class.")

    def to_dict(self) -> dict[str, str | None]:
        return {"recipe": self.recipe, "target_test_class": self.target_test_class}

    @classmethod
    def from_dict(cls, payload: object) -> "VerificationContract":
        if not isinstance(payload, dict):
            raise ValueError("verification contract must be an object.")
        recipe = payload.get("recipe")
        target_test_class = payload.get("target_test_class")
        if not isinstance(recipe, str) or (target_test_class is not None and not isinstance(target_test_class, str)):
            raise ValueError("verification contract fields are invalid.")
        return cls(recipe, target_test_class)


@dataclass(frozen=True, slots=True)
class TaskBudget:
    """服务端在任务开始时冻结的模型资源上限，模型和前端均不能自行放宽。"""

    max_total_tokens: int | None = None
    max_estimated_cost: float | None = None
    currency: str | None = None

    def __post_init__(self) -> None:
        if self.max_total_tokens is not None and self.max_total_tokens < 1:
            raise ValueError("max_total_tokens must be positive when configured.")
        if self.max_estimated_cost is not None and self.max_estimated_cost < 0:
            raise ValueError("max_estimated_cost must not be negative.")
        if self.max_estimated_cost is not None and not self.currency:
            raise ValueError("cost budget requires a currency.")

    @property
    def configured(self) -> bool:
        return self.max_total_tokens is not None or self.max_estimated_cost is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "max_total_tokens": self.max_total_tokens,
            "max_estimated_cost": self.max_estimated_cost,
            "currency": self.currency,
            "configured": self.configured,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "TaskBudget":
        if not isinstance(raw, dict):
            raise ValueError("task budget must be an object.")
        tokens = raw.get("max_total_tokens")
        cost = raw.get("max_estimated_cost")
        currency = raw.get("currency")
        if tokens is not None and (not isinstance(tokens, int) or isinstance(tokens, bool)):
            raise ValueError("max_total_tokens must be an integer.")
        if cost is not None and (not isinstance(cost, (int, float)) or isinstance(cost, bool)):
            raise ValueError("max_estimated_cost must be a number.")
        if currency is not None and not isinstance(currency, str):
            raise ValueError("currency must be a string.")
        return cls(tokens, float(cost) if cost is not None else None, currency)

    def restricted_by(self, policy: "TaskBudget") -> "TaskBudget":
        """合并任务请求与服务端策略，只能收紧，绝不允许请求放宽服务端上限。"""

        if self.max_estimated_cost is not None and policy.max_estimated_cost is not None and self.currency != policy.currency:
            raise ValueError("task budget currency conflicts with server policy.")
        token_limits = [value for value in (self.max_total_tokens, policy.max_total_tokens) if value is not None]
        cost_limits = [value for value in (self.max_estimated_cost, policy.max_estimated_cost) if value is not None]
        return TaskBudget(
            max_total_tokens=min(token_limits) if token_limits else None,
            max_estimated_cost=min(cost_limits) if cost_limits else None,
            currency=(policy.currency or self.currency) if cost_limits else None,
        )


@dataclass(frozen=True, slots=True)
class TaskRequest:
    repository: Path
    description: str
    output_root: Path = field(default_factory=default_output_root)
    task_id: str = field(default_factory=lambda: f"task-{uuid4().hex[:12]}")
    max_steps: int = 12
    project_id: str | None = None
    workspace_selection: WorkspaceSelection = field(default_factory=WorkspaceSelection)
    verification_contract: VerificationContract | None = None
    approved_mcp_tools: tuple[str, ...] = ()
    budget: TaskBudget = field(default_factory=TaskBudget)
    operation: TaskOperation = TaskOperation.CHANGE

    def __post_init__(self) -> None:
        if not self.description.strip():
            raise ValueError("Task description must not be blank.")
        if not isinstance(self.operation, TaskOperation):
            raise ValueError("Task operation must be change or research.")
        if self.max_steps < 1:
            raise ValueError("max_steps must be at least 1.")
        if len(self.approved_mcp_tools) > 64:
            raise ValueError("approved_mcp_tools supports at most 64 tools.")
        if any(
            not isinstance(capability_id, str)
            or not capability_id.startswith("mcp__")
            or len(capability_id) > 255
            for capability_id in self.approved_mcp_tools
        ):
            raise ValueError("approved_mcp_tools must contain MCP capability IDs.")

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
        # 任务快照使用确定性顺序，避免同一授权集合产生不同恢复状态。
        object.__setattr__(self, "approved_mcp_tools", tuple(sorted(set(self.approved_mcp_tools))))


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
