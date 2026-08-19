"""复杂编码任务的受控并行只读子 Agent。

阶段 2（2.1）升级：从「三个硬编码角色」升级为「可声明 fan-out 的编排抽象」：

- ``SubagentSpec`` 声明角色、目标、执行函数、参数、超时与引用上限；
- 每个子 Agent 的产出先经过 ``SubagentOutput`` schema 校验，违规产出被标记 ``BLOCKED``
  而非污染父任务状态；
- 子 Agent 永远使用 ``safe`` 权限与只读工具，不能继承 Shell、MCP、补丁或写权限；
- 单个子 Agent 有独立超时，超时被回收为 ``BLOCKED`` 而不是无限挂起。
"""

from __future__ import annotations

import re
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError

from repopilot_guard.permissions import PermissionGrant
from repopilot_guard.repository_tools import RepositoryTools


MAX_SUBAGENTS = 3
MAX_SUBAGENT_REFERENCES = 20
DEFAULT_SUBAGENT_TIMEOUT_SECONDS = 30.0
MAX_SUBAGENT_SUMMARY_CHARS = 4000


class SubagentReference(BaseModel):
    """子 Agent 引用的单条证据；只保存可审计元数据，绝不保存文件正文。"""

    source_type: str = Field(min_length=1)
    path: str = Field(min_length=1)
    line_start: int = Field(default=0, ge=0)
    line_end: int = Field(default=0, ge=0)
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        return self.model_dump()


class SubagentOutput(BaseModel):
    """每个子 Agent 必须产出的结构化结果契约；违规产出在回收时被拒绝。"""

    summary: str = Field(min_length=1, max_length=MAX_SUBAGENT_SUMMARY_CHARS)
    references: list[SubagentReference] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SubagentSpec:
    """一个可声明子 Agent 的规格：角色、目标、执行函数、参数、超时与引用上限。"""

    role: str
    goal: str
    function: Callable[..., tuple[str, list[dict[str, object]]]]
    arguments: tuple[object, ...] = ()
    timeout_seconds: float = DEFAULT_SUBAGENT_TIMEOUT_SECONDS
    max_references: int = MAX_SUBAGENT_REFERENCES


@dataclass(frozen=True, slots=True)
class SubagentFinding:
    """子 Agent 返回给父任务的可引用摘要，不携带文件全文。"""

    subagent_id: str
    role: str
    status: str
    summary: str
    references: tuple[dict[str, object], ...]
    duration_ms: int

    def to_dict(self) -> dict[str, object]:
        return {
            "subagent_id": self.subagent_id,
            "role": self.role,
            "status": self.status,
            "summary": self.summary,
            "references": [dict(item) for item in self.references],
            "duration_ms": self.duration_ms,
            "capabilities": ["list_files", "search_code", "inspect_build"],
            "permission_mode": "safe",
        }


@dataclass(frozen=True, slots=True)
class SubagentRunResult:
    status: str
    code: str
    findings: tuple[SubagentFinding, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "code": self.code,
            "parallelism": len(self.findings),
            "findings": [finding.to_dict() for finding in self.findings],
        }


class SubagentCoordinator:
    """将复杂任务拆成互不写入的研究子 Agent，并在本机线程池并行执行。"""

    def should_delegate(self, task_description: str, candidate_files: list[str]) -> bool:
        normalized = task_description.strip()
        complexity_terms = ("同时", "并且", "跨", "重构", "多个", "全局", "链路", "模块")
        return len(normalized) >= 72 or len(candidate_files) >= 3 or sum(term in normalized for term in complexity_terms) >= 2

    def default_specs(self, task_description: str, candidate_files: list[str]) -> tuple[SubagentSpec, ...]:
        """默认三角色：结构、实现、验证。调用方可声明自己的 specs 以扩展 fan-out。"""

        return (
            SubagentSpec("repository_mapper", "梳理模块、文件和工程入口", self._map_repository),
            SubagentSpec(
                "implementation_researcher",
                "根据任务描述搜索候选实现",
                self._research_implementation,
                (task_description, candidate_files),
            ),
            SubagentSpec("verification_researcher", "识别构建描述、测试位置和验证入口", self._research_verification),
        )

    def run(
        self,
        *,
        task_description: str,
        workspace_root: Path,
        candidate_files: list[str],
    ) -> SubagentRunResult:
        if not self.should_delegate(task_description, candidate_files):
            return SubagentRunResult("READY", "SUBAGENTS_NOT_REQUIRED")
        return self.run_specs(self.default_specs(task_description, candidate_files), workspace_root=workspace_root)

    def run_specs(self, specs: tuple[SubagentSpec, ...], *, workspace_root: Path) -> SubagentRunResult:
        """按声明的 specs 并行执行，逐项回收并校验产出；每个 spec 有独立超时。"""

        if not specs:
            return SubagentRunResult("READY", "SUBAGENTS_NOT_REQUIRED")
        # 子 Agent 永远使用安全权限，不能继承父任务的 Shell、MCP、补丁或 Git 能力。
        tools = RepositoryTools(workspace_root, PermissionGrant.safe())
        findings: list[SubagentFinding] = []
        with ThreadPoolExecutor(max_workers=MAX_SUBAGENTS, thread_name_prefix="repopilot-subagent") as executor:
            pending: dict[Future, tuple[str, SubagentSpec, float]] = {}
            for spec in specs[:MAX_SUBAGENTS]:
                subagent_id = f"subagent-{uuid4().hex[:12]}"
                pending[executor.submit(spec.function, tools, *spec.arguments)] = (subagent_id, spec, time.monotonic())
            # 直接对每个 future 调用带超时的 result()：as_completed 会一直等到完成，无法触发超时。
            for future, (subagent_id, spec, started) in pending.items():
                findings.append(self._collect(future, subagent_id, spec, started))
        findings.sort(key=lambda item: item.role)
        code = "SUBAGENTS_COMPLETED" if all(item.status == "READY" for item in findings) else "SUBAGENTS_PARTIAL"
        return SubagentRunResult("READY", code, tuple(findings))

    @staticmethod
    def _collect(future: Future, subagent_id: str, spec: SubagentSpec, started: float) -> SubagentFinding:
        duration_ms = int((time.monotonic() - started) * 1000)
        try:
            raw = future.result(timeout=spec.timeout_seconds)
            summary, references = _validate_output(raw, spec)
            return SubagentFinding(subagent_id, spec.role, "READY", summary, references, duration_ms)
        except TimeoutError:
            return SubagentFinding(
                subagent_id,
                spec.role,
                "BLOCKED",
                f"子 Agent 超时（>{spec.timeout_seconds:.0f}s），已回收以保护父任务。",
                (),
                duration_ms,
            )
        except Exception as error:
            return SubagentFinding(
                subagent_id,
                spec.role,
                "BLOCKED",
                f"子 Agent 读取研究失败：{type(error).__name__}。",
                (),
                duration_ms,
            )

    @staticmethod
    def _map_repository(tools: RepositoryTools) -> tuple[str, list[dict[str, object]]]:
        result = tools.list_files(max_depth=4, max_results=80)
        files = result.data.get("files", []) if result.status == "READY" else []
        references = [
            {"source_type": "subagent_repository_map", "path": path, "line_start": 0, "line_end": 0, "note": "仓库结构"}
            for path in files[:MAX_SUBAGENT_REFERENCES]
            if isinstance(path, str)
        ]
        return f"仓库结构研究完成，发现 {len(files)} 个允许范围内文件。", references

    @staticmethod
    def _research_implementation(
        tools: RepositoryTools,
        task_description: str,
        candidate_files: list[str],
    ) -> tuple[str, list[dict[str, object]]]:
        terms = _search_terms(task_description)
        references: list[dict[str, object]] = []
        for term in terms[:3]:
            result = tools.search_code(term, max_results=8, max_depth=6)
            for match in result.data.get("matches", []) if result.status == "READY" else []:
                if not isinstance(match, dict) or not isinstance(match.get("path"), str):
                    continue
                references.append(
                    {
                        "source_type": "subagent_code_search",
                        "path": match["path"],
                        "line_start": int(match.get("line", 0)),
                        "line_end": int(match.get("line", 0)),
                        "note": f"关键词 {term}",
                    }
                )
                if len(references) >= MAX_SUBAGENT_REFERENCES:
                    break
            if len(references) >= MAX_SUBAGENT_REFERENCES:
                break
        for path in candidate_files[:3]:
            if not isinstance(path, str) or not path:
                continue
            references.append(
                {"source_type": "subagent_candidate", "path": path, "line_start": 0, "line_end": 0, "note": "父任务候选文件"}
            )
        deduplicated = _deduplicate_references(references)
        return f"实现研究完成，基于 {len(terms)} 个任务关键词定位到 {len(deduplicated)} 条候选证据。", deduplicated

    @staticmethod
    def _research_verification(tools: RepositoryTools) -> tuple[str, list[dict[str, object]]]:
        result = tools.inspect_build()
        descriptors = result.data.get("build_descriptors", []) if result.status == "READY" else []
        references = [
            {"source_type": "subagent_build", "path": path, "line_start": 0, "line_end": 0, "note": "构建描述符"}
            for path in descriptors[:MAX_SUBAGENT_REFERENCES]
            if isinstance(path, str)
        ]
        tests = tools.search_code("@Test", max_results=12, max_depth=8)
        for match in tests.data.get("matches", []) if tests.status == "READY" else []:
            if isinstance(match, dict) and isinstance(match.get("path"), str):
                references.append(
                    {
                        "source_type": "subagent_test_search",
                        "path": match["path"],
                        "line_start": int(match.get("line", 0)),
                        "line_end": int(match.get("line", 0)),
                        "note": "现有测试",
                    }
                )
        deduplicated = _deduplicate_references(references)
        return f"验证研究完成，发现 {len(descriptors)} 个构建描述符和 {len(deduplicated)} 条验证证据。", deduplicated


def _validate_output(raw: object, spec: SubagentSpec) -> tuple[str, tuple[dict[str, object], ...]]:
    """集中校验子 Agent 产出；任何不符合契约的产出都被拒绝为 BLOCKED。"""

    if not isinstance(raw, tuple) or len(raw) != 2:
        raise ValueError("SUBAGENT_OUTPUT_INVALID_SHAPE")
    summary, references = raw
    try:
        output = SubagentOutput.model_validate(
            {
                "summary": summary if isinstance(summary, str) else "",
                "references": references if isinstance(references, list) else [],
            }
        )
    except ValidationError as error:
        raise ValueError("SUBAGENT_OUTPUT_INVALID_SCHEMA") from error
    references_out = tuple(reference.to_dict() for reference in output.references[:spec.max_references])
    return output.summary, references_out


def _search_terms(description: str) -> tuple[str, ...]:
    terms = re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", description)
    return tuple(dict.fromkeys(terms)) or ("TODO",)


def _deduplicate_references(references: list[dict[str, object]]) -> list[dict[str, object]]:
    deduplicated: list[dict[str, object]] = []
    seen: set[tuple[object, object, object]] = set()
    for reference in references:
        key = (reference.get("path"), reference.get("line_start"), reference.get("line_end"))
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(reference)
        if len(deduplicated) >= MAX_SUBAGENT_REFERENCES:
            break
    return deduplicated
