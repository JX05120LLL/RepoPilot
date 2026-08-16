"""已授权项目的 Agent 能力档案。

档案是对项目根目录进行受限、只读扫描后得到的可审计事实。它只读取标准构建
描述和少量候选入口源码，不运行项目命令，也不把源码正文写进 SQLite；用户确认的业务约束与禁改
路径会在后续任务中作为不可信但可引用的上下文注入。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import xml.etree.ElementTree as element_tree
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from repopilot_guard.project_profiles import ProjectProfileDetector


_MAX_ENTRYPOINT_FILES = 24
_MAX_MODULES = 32
_MAX_SCANNED_SOURCE_FILES = 2_000
_PROTECTED_PATHS = (
    ".env",
    ".env.*",
    ".git/**",
    "**/*secret*",
    "**/*credential*",
    "**/application-prod.*",
    "**/db/migration/**",
)
_SOURCE_SUFFIXES = {".java", ".py", ".ts", ".tsx", ".js", ".jsx"}


@dataclass(frozen=True, slots=True)
class CapabilityProfile:
    project_id: str
    facts: dict[str, object]
    profile_sha256: str
    confirmed_at: str | None = None
    business_rules: tuple[str, ...] = ()
    protected_paths: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        return "CONFIRMED" if self.confirmed_at else "PENDING_CONFIRMATION"

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "project_id": self.project_id,
            "profile_sha256": self.profile_sha256,
            "confirmed_at": self.confirmed_at,
            "facts": self.facts,
            "business_rules": list(self.business_rules),
            "protected_paths": list(self.protected_paths),
        }

    def context_payload(self) -> dict[str, object]:
        """返回可进入模型的最小快照，绝不返回本机绝对路径或源码正文。"""

        return {
            "status": self.status,
            "profile_sha256": self.profile_sha256,
            "modules": self.facts["modules"],
            "entrypoints": self.facts["entrypoints"],
            "verification": self.facts["verification"],
            "protected_paths": list(dict.fromkeys((*self.facts["protected_paths"], *self.protected_paths))),
            "business_rules": list(self.business_rules),
            "known_limitations": self.facts["known_limitations"],
        }


class CapabilityProfileScanner:
    """只使用受控文件名、构建描述和少量源文件名生成项目事实。"""

    def scan(self, project_id: str, repository: Path) -> CapabilityProfile:
        root = repository.expanduser().resolve()
        if not root.is_dir():
            raise ValueError("PROJECT_DIRECTORY_NOT_FOUND")
        profiles = ProjectProfileDetector().detect(root)
        facts: dict[str, object] = {
            "schema_version": 1,
            "modules": _modules(root),
            "entrypoints": _entrypoints(root),
            "verification": [
                {
                    "profile_id": item.profile_id,
                    "display_name": item.display_name,
                    "detected_files": list(item.detected_files),
                    "execution_supported": item.execution_supported,
                }
                for item in profiles
            ],
            "protected_paths": list(_PROTECTED_PATHS),
            "known_limitations": [
                "档案来自只读静态扫描；启动命令、测试数据和业务规则须由用户确认后才可作为任务约束。",
                "档案不授予工具权限，也不替代 PolicyGuard、审批或固定验证 Recipe。",
            ],
        }
        digest = _digest(facts)
        return CapabilityProfile(project_id=project_id, facts=facts, profile_sha256=digest)


def normalize_confirmations(values: Iterable[str], *, code: str) -> tuple[str, ...]:
    normalized = tuple(dict.fromkeys(" ".join(value.split()) for value in values if isinstance(value, str) and value.strip()))
    if len(normalized) > 32 or any(len(value) > 280 for value in normalized):
        raise ValueError(code)
    return normalized


def _modules(root: Path) -> list[dict[str, str]]:
    candidates = [root]
    pom = root / "pom.xml"
    if pom.is_file():
        try:
            tree = element_tree.parse(pom)
            for module in tree.findall(".//{*}module"):
                value = (module.text or "").strip()
                candidate = (root / value).resolve()
                if value and candidate.is_dir() and candidate.is_relative_to(root):
                    candidates.append(candidate)
        except element_tree.ParseError:
            pass
    result: list[dict[str, str]] = []
    for candidate in candidates[:_MAX_MODULES]:
        relative = "." if candidate == root else candidate.relative_to(root).as_posix()
        descriptors = [name for name in ("pom.xml", "build.gradle", "build.gradle.kts", "package.json", "pyproject.toml") if (candidate / name).is_file()]
        result.append({"path": relative, "descriptors": ", ".join(descriptors) or "directory"})
    return result


def _entrypoints(root: Path) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    ignored = {".git", ".repopilot", "node_modules", "target", "build", "dist", ".venv"}
    inspected = 0
    for directory, directories, names in os.walk(root, topdown=True, followlinks=False):
        directories[:] = sorted(name for name in directories if name not in ignored)
        for name in sorted(names):
            path = Path(directory) / name
            if path.suffix.lower() not in _SOURCE_SUFFIXES:
                continue
            inspected += 1
            if inspected > _MAX_SCANNED_SOURCE_FILES or len(entries) >= _MAX_ENTRYPOINT_FILES:
                return entries
            if name.endswith(("Controller.java", "Application.java", "App.java")) or name in {"main.py", "app.py", "server.py", "index.ts", "index.js"}:
                entry = {"path": path.relative_to(root).as_posix(), "kind": _entrypoint_kind(name)}
                if entry["kind"] == "http_controller":
                    targets = _controller_targets(path)
                    if targets:
                        entry["targets"] = ", ".join(targets)
                entries.append(entry)
    return entries


def _entrypoint_kind(name: str) -> str:
    if name.endswith("Controller.java"):
        return "http_controller"
    if name.endswith("Application.java") or name in {"main.py", "app.py", "server.py"}:
        return "application_entry"
    return "application_entry_candidate"


def _controller_targets(path: Path) -> tuple[str, ...]:
    """仅从单个 Controller 的有限正文提取 Service 标识，不保存正文或方法参数。"""

    try:
        content = path.read_text(encoding="utf-8", errors="replace")[:32_000]
    except OSError:
        return ()
    return tuple(sorted(set(re.findall(r"\b([A-Z][A-Za-z0-9_]*Service)\b", content))))[:8]


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
