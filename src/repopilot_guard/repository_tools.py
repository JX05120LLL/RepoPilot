"""受 PolicyGuard 保护的只读仓库工具。"""

from __future__ import annotations

import os
import xml.etree.ElementTree as element_tree
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from repopilot_guard.evidence import EvidenceStore
from repopilot_guard.permissions import PermissionGrant
from repopilot_guard.policy import PolicyGuard, ToolName
from repopilot_guard.preflight import PreflightInspector


@dataclass(frozen=True, slots=True)
class ToolResult:
    """所有只读工具共用的可审计返回值。"""

    status: str
    code: str
    message: str
    data: dict[str, Any]

    def to_dict(self) -> dict[str, object]:
        return {"status": self.status, "code": self.code, "message": self.message, "data": self.data}


class RepositoryTools:
    """限制范围、大小与返回数量的仓库读取工具。"""

    def __init__(
        self,
        workspace_root: Path,
        permission: PermissionGrant,
        evidence: EvidenceStore | None = None,
    ) -> None:
        self.workspace_root = workspace_root.expanduser().resolve()
        self.permission = permission
        self.guard = PolicyGuard(self.workspace_root, permission)
        self.evidence = evidence

    def list_files(self, path: Path = Path("."), max_depth: int = 6, max_results: int = 200) -> ToolResult:
        if max_depth < 0 or max_results < 1:
            return self._blocked("INVALID_LIMIT", "目录深度和结果数量必须为有效正数。", {"path": str(path)})
        target = self._resolve(path)
        decision = self.guard.check_path(ToolName.LIST_FILES, target)
        if not decision.allowed:
            return self._blocked(decision.audit_code, decision.reason, {"path": str(target)})
        if not target.is_dir():
            return self._blocked("NOT_A_DIRECTORY", "目标路径不是目录。", {"path": str(target)})

        files: list[str] = []
        for candidate in self._iter_files(target, ToolName.LIST_FILES, max_depth=max_depth, max_candidates=max_results):
            files.append(self._display_path(candidate))
            if len(files) >= max_results:
                break
        return self._ready(
            "FILES_LISTED",
            "已返回允许范围内的文件列表。",
            {"path": self._display_path(target), "files": files, "truncated": len(files) >= max_results},
        )

    def search_code(
        self,
        query: str,
        path: Path = Path("."),
        max_results: int = 100,
        max_depth: int = 6,
    ) -> ToolResult:
        if not query:
            return self._blocked("EMPTY_QUERY", "搜索内容不能为空。", {})
        if max_results < 1 or max_depth < 0:
            return self._blocked("INVALID_LIMIT", "搜索深度和结果数量必须为有效正数。", {})
        target = self._resolve(path)
        decision = self.guard.check_path(ToolName.SEARCH_CODE, target)
        if not decision.allowed:
            return self._blocked(decision.audit_code, decision.reason, {"path": str(target)})
        if not target.is_dir():
            return self._blocked("NOT_A_DIRECTORY", "搜索目标必须是目录。", {"path": str(target)})

        matches: list[dict[str, object]] = []
        for candidate in self._iter_files(target, ToolName.SEARCH_CODE, max_depth=max_depth, max_candidates=1000):
            try:
                if candidate.stat().st_size > 256 * 1024:
                    continue
            except OSError:
                continue
            try:
                content = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for line_number, line in enumerate(content.splitlines(), start=1):
                if query not in line:
                    continue
                matches.append(
                    {
                        "path": self._display_path(candidate),
                        "line": line_number,
                        "content": line[:300],
                    }
                )
                if len(matches) >= max_results:
                    return self._ready(
                        "SEARCH_COMPLETED",
                        "搜索结果已达到上限并截断。",
                        {"query": query, "matches": matches, "truncated": True},
                    )
        return self._ready(
            "SEARCH_COMPLETED",
            "字面量搜索完成。",
            {"query": query, "matches": matches, "truncated": False},
        )

    def read_file(self, path: Path, max_bytes: int = 256 * 1024) -> ToolResult:
        if max_bytes < 1:
            return self._blocked("INVALID_LIMIT", "文件大小上限必须为正数。", {})
        target = self._resolve(path)
        decision = self.guard.check_path(ToolName.READ_FILE, target)
        if not decision.allowed:
            return self._blocked(decision.audit_code, decision.reason, {"path": str(target)})
        if not target.is_file():
            return self._blocked("NOT_A_FILE", "目标路径不是文件。", {"path": str(target)})
        try:
            size = target.stat().st_size
        except OSError:
            return self._blocked("FILE_UNAVAILABLE", "无法读取目标文件属性。", {"path": self._display_path(target)})
        if size > max_bytes:
            return self._blocked("FILE_TOO_LARGE", "文件超过读取大小上限。", {"path": self._display_path(target)})
        try:
            raw = target.read_bytes()
        except OSError:
            return self._blocked("FILE_UNAVAILABLE", "无法读取目标文件。", {"path": self._display_path(target)})
        if b"\0" in raw:
            return self._blocked("BINARY_FILE", "二进制文件不允许作为代码上下文读取。", {"path": self._display_path(target)})
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            return self._blocked("UNSUPPORTED_ENCODING", "仅支持 UTF-8 文本文件。", {"path": self._display_path(target)})
        return self._ready(
            "FILE_READ",
            "文件读取完成。",
            {"path": self._display_path(target), "content": content, "bytes": len(raw)},
        )

    def inspect_build(self) -> ToolResult:
        pom_path = self.workspace_root / "pom.xml"
        decision = self.guard.check_path(ToolName.INSPECT_BUILD, pom_path)
        if not decision.allowed:
            return self._blocked(decision.audit_code, decision.reason, {"path": str(pom_path)})
        preflight = PreflightInspector().inspect(self.workspace_root)
        pom_summary: dict[str, object] = {}
        if pom_path.is_file():
            try:
                root = element_tree.parse(pom_path).getroot()
                namespace = {"m": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}
                prefix = "m:" if namespace else ""
                pom_summary = {
                    "group_id": self._xml_text(root, f"{prefix}groupId", namespace),
                    "artifact_id": self._xml_text(root, f"{prefix}artifactId", namespace),
                    "version": self._xml_text(root, f"{prefix}version", namespace),
                    "modules": [
                        item.text.strip()
                        for item in root.findall(f"{prefix}modules/{prefix}module", namespace)
                        if item.text and item.text.strip()
                    ],
                }
            except element_tree.ParseError:
                pom_summary = {"parse_error": "pom.xml 不是可解析的 XML。"}
        return self._ready(
            "BUILD_INSPECTED",
            "Maven 构建信息检查完成，未执行 Maven。",
            {"preflight": preflight.to_dict(), "pom": pom_summary},
        )

    @staticmethod
    def _xml_text(root: element_tree.Element, name: str, namespace: dict[str, str]) -> str | None:
        item = root.find(name, namespace)
        return item.text.strip() if item is not None and item.text else None

    def _resolve(self, path: Path) -> Path:
        return path.expanduser().resolve() if path.is_absolute() else (self.workspace_root / path).resolve()

    def _display_path(self, path: Path) -> str:
        try:
            return path.relative_to(self.workspace_root).as_posix()
        except ValueError:
            return str(path)

    def _iter_files(
        self,
        target: Path,
        tool: ToolName,
        *,
        max_depth: int,
        max_candidates: int,
    ) -> list[Path]:
        """在进入目录前做策略与深度剪枝，避免扫描整个大型仓库。"""

        files: list[Path] = []
        for directory, directory_names, file_names in os.walk(target, topdown=True, followlinks=False):
            current = Path(directory)
            current_depth = len(current.relative_to(target).parts)
            directory_names[:] = sorted(
                name
                for name in directory_names
                if current_depth < max_depth and self.guard.check_path(tool, current / name).allowed
            )
            for name in sorted(file_names):
                candidate = current / name
                if not self.guard.check_path(tool, candidate).allowed:
                    continue
                files.append(candidate)
                if len(files) >= max_candidates:
                    return files
        return files

    def _ready(self, code: str, message: str, data: dict[str, Any]) -> ToolResult:
        result = ToolResult("READY", code, message, data)
        self._record("repository_tool_completed", result)
        return result

    def _blocked(self, code: str, message: str, data: dict[str, Any]) -> ToolResult:
        result = ToolResult("BLOCKED", code, message, data)
        self._record("repository_tool_blocked", result)
        return result

    def _record(self, event_type: str, result: ToolResult) -> None:
        if self.evidence:
            self.evidence.record(event_type, result.to_dict())
