"""复杂编码任务的受控并行只读子 Agent。"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from repopilot_guard.permissions import PermissionGrant
from repopilot_guard.repository_tools import RepositoryTools


MAX_SUBAGENTS = 3
MAX_SUBAGENT_REFERENCES = 20


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
    """将复杂任务固定拆成互不写入的研究角色，并在本机线程池并行执行。"""

    def should_delegate(self, task_description: str, candidate_files: list[str]) -> bool:
        normalized = task_description.strip()
        complexity_terms = ("同时", "并且", "跨", "重构", "多个", "全局", "链路", "模块")
        return len(normalized) >= 72 or len(candidate_files) >= 3 or sum(term in normalized for term in complexity_terms) >= 2

    def run(
        self,
        *,
        task_description: str,
        workspace_root: Path,
        candidate_files: list[str],
    ) -> SubagentRunResult:
        if not self.should_delegate(task_description, candidate_files):
            return SubagentRunResult("READY", "SUBAGENTS_NOT_REQUIRED")

        # 子 Agent 永远使用安全权限，不能继承父任务的 Shell、MCP、补丁或 Git 能力。
        tools = RepositoryTools(workspace_root, PermissionGrant.safe())
        jobs = (
            ("repository_mapper", self._map_repository, (tools,)),
            ("implementation_researcher", self._research_implementation, (tools, task_description, candidate_files)),
            ("verification_researcher", self._research_verification, (tools,)),
        )
        findings: list[SubagentFinding] = []
        with ThreadPoolExecutor(max_workers=MAX_SUBAGENTS, thread_name_prefix="repopilot-subagent") as executor:
            futures = {}
            for role, callback, arguments in jobs:
                subagent_id = f"subagent-{uuid4().hex[:12]}"
                futures[executor.submit(callback, *arguments)] = (subagent_id, role, time.monotonic())
            for future in as_completed(futures):
                subagent_id, role, started = futures[future]
                try:
                    summary, references = future.result()
                    finding = SubagentFinding(subagent_id, role, "READY", summary, tuple(references), int((time.monotonic() - started) * 1000))
                except Exception as error:
                    finding = SubagentFinding(
                        subagent_id,
                        role,
                        "BLOCKED",
                        f"子 Agent 读取研究失败：{type(error).__name__}。",
                        (),
                        int((time.monotonic() - started) * 1000),
                    )
                findings.append(finding)
        findings.sort(key=lambda item: item.role)
        code = "SUBAGENTS_COMPLETED" if all(item.status == "READY" for item in findings) else "SUBAGENTS_PARTIAL"
        return SubagentRunResult("READY", code, tuple(findings))

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
