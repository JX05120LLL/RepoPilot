"""兼容 Agent Skills 约定的 SKILL.md 发现与渐进加载。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml
from yaml.tokens import AliasToken, AnchorToken

from repopilot_guard.capabilities import (
    CapabilityDescriptor,
    CapabilityKind,
    CapabilityRisk,
    CapabilityScope,
)


MAX_SKILL_BYTES = 128 * 1024
MAX_SKILLS = 512
DEFAULT_CATALOG_CHARS = 8_000
_SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SCOPE_PRIORITY = {
    CapabilityScope.BUNDLED: 1,
    CapabilityScope.PLUGIN: 2,
    CapabilityScope.USER: 3,
    CapabilityScope.PROJECT: 4,
}


class SkillError(ValueError):
    """对外只暴露稳定错误码和路径，不回显 Skill 正文。"""

    def __init__(self, code: str, path: Path, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.path = path
        self.message = message

    def to_dict(self) -> dict[str, object]:
        return {"code": self.code, "path": str(self.path), "message": self.message}


@dataclass(frozen=True, slots=True)
class SkillManifest:
    """发现阶段可进入模型上下文的少量 Skill 元数据。"""

    name: str
    description: str
    path: Path
    root: Path
    scope: CapabilityScope
    allowed_tools: tuple[str, ...]
    user_invocable: bool
    disable_model_invocation: bool
    compatibility: str | None
    content_sha256: str

    def catalog_dict(self) -> dict[str, object]:
        # 渐进披露阶段不返回正文、脚本或参考文件内容。
        return {
            "name": self.name,
            "description": self.description,
            "path": str(self.path),
            "scope": self.scope.value,
        }

    def detail_dict(self) -> dict[str, object]:
        return {
            **self.catalog_dict(),
            "allowed_tools": list(self.allowed_tools),
            "user_invocable": self.user_invocable,
            "disable_model_invocation": self.disable_model_invocation,
            "compatibility": self.compatibility,
            "content_sha256": self.content_sha256,
        }

    def capability(self) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            capability_id=f"skill__{self.name}",
            name=self.name,
            description=self.description,
            kind=CapabilityKind.SKILL,
            scope=self.scope,
            source=str(self.path),
            risks=frozenset({CapabilityRisk.READ}),
            metadata={
                "allowed_tools": list(self.allowed_tools),
                "user_invocable": self.user_invocable,
                "disable_model_invocation": self.disable_model_invocation,
                "content_sha256": self.content_sha256,
            },
        )


@dataclass(frozen=True, slots=True)
class LoadedSkill:
    """只有 Skill 被显式或语义选中后才创建。"""

    manifest: SkillManifest
    instructions: str

    def to_dict(self) -> dict[str, object]:
        return {
            "manifest": self.manifest.detail_dict(),
            "instructions": self.instructions,
            "security_label": "UNTRUSTED_SKILL_INSTRUCTIONS",
        }


@dataclass(frozen=True, slots=True)
class SkillCatalog:
    entries: tuple[SkillManifest, ...]
    issues: tuple[SkillError, ...]
    truncated: bool
    omitted_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "status": "READY",
            "skills": [item.catalog_dict() for item in self.entries],
            "issues": [item.to_dict() for item in self.issues],
            "truncated": self.truncated,
            "omitted_count": self.omitted_count,
        }


class SkillRegistry:
    """按 bundled < user < project 的优先级去重并加载 Skill。"""

    def __init__(self, manifests: Iterable[SkillManifest], issues: Iterable[SkillError] = ()) -> None:
        selected: dict[str, SkillManifest] = {}
        collected_issues = list(issues)
        for manifest in sorted(manifests, key=lambda item: (_SCOPE_PRIORITY[item.scope], str(item.path))):
            previous = selected.get(manifest.name)
            if previous is not None and _SCOPE_PRIORITY[previous.scope] == _SCOPE_PRIORITY[manifest.scope]:
                collected_issues.append(
                    SkillError("DUPLICATE_SKILL_IN_SCOPE", manifest.path, "同一作用域存在同名 Skill，已保留路径排序靠前者。")
                )
                continue
            selected[manifest.name] = manifest
        self._manifests = selected
        self._issues = tuple(collected_issues)

    @classmethod
    def discover(
        cls,
        *,
        project_root: Path | None = None,
        user_roots: Iterable[Path] = (),
        plugin_roots: Iterable[Path] = (),
        bundled_roots: Iterable[Path] = (),
    ) -> "SkillRegistry":
        roots: list[tuple[Path, CapabilityScope]] = []
        roots.extend((Path(root), CapabilityScope.BUNDLED) for root in bundled_roots)
        roots.extend((Path(root), CapabilityScope.PLUGIN) for root in plugin_roots)
        roots.extend((Path(root), CapabilityScope.USER) for root in user_roots)
        if project_root is not None:
            project = project_root.expanduser().resolve()
            roots.extend(
                (
                    (project / ".agents" / "skills", CapabilityScope.PROJECT),
                    (project / ".repopilot" / "skills", CapabilityScope.PROJECT),
                )
            )

        manifests: list[SkillManifest] = []
        issues: list[SkillError] = []
        discovered = 0
        for raw_root, scope in roots:
            root = raw_root.expanduser().resolve()
            if not root.exists():
                continue
            if not root.is_dir():
                issues.append(SkillError("SKILL_ROOT_NOT_DIRECTORY", root, "Skill 根路径不是目录。"))
                continue
            for path in sorted(root.rglob("SKILL.md")):
                discovered += 1
                if discovered > MAX_SKILLS:
                    issues.append(SkillError("SKILL_DISCOVERY_LIMIT_REACHED", root, "Skill 数量超过安全上限。"))
                    return cls(manifests, issues)
                try:
                    manifests.append(_read_manifest(path, root, scope))
                except SkillError as error:
                    issues.append(error)
        return cls(manifests, issues)

    @property
    def issues(self) -> tuple[SkillError, ...]:
        return self._issues

    def manifest(self, name: str) -> SkillManifest | None:
        return self._manifests.get(name)

    def catalog(self, max_chars: int = DEFAULT_CATALOG_CHARS) -> SkillCatalog:
        if max_chars <= 0:
            raise ValueError("INVALID_SKILL_CATALOG_BUDGET")
        included: list[SkillManifest] = []
        used = 0
        all_manifests = tuple(sorted(self._manifests.values(), key=lambda item: item.name))
        for manifest in all_manifests:
            encoded = json.dumps(manifest.catalog_dict(), ensure_ascii=False, separators=(",", ":"))
            if used + len(encoded) > max_chars:
                break
            included.append(manifest)
            used += len(encoded)
        omitted = len(all_manifests) - len(included)
        return SkillCatalog(tuple(included), self._issues, omitted > 0, omitted)

    def load(self, name: str) -> LoadedSkill:
        manifest = self._manifests.get(name)
        if manifest is None:
            raise SkillError("SKILL_NOT_FOUND", Path(name), "Skill 未发现或已被禁用。")
        raw = _read_skill_bytes(manifest.path, manifest.root)
        if hashlib.sha256(raw).hexdigest() != manifest.content_sha256:
            raise SkillError("SKILL_CHANGED_AFTER_DISCOVERY", manifest.path, "Skill 在发现后发生变化，请重新发现。")
        _, instructions = _parse_skill_document(raw, manifest.path)
        return LoadedSkill(manifest, instructions)

    def capabilities(self) -> tuple[CapabilityDescriptor, ...]:
        return tuple(item.capability() for item in sorted(self._manifests.values(), key=lambda entry: entry.name))

    def manifests(self) -> tuple[SkillManifest, ...]:
        """返回已完成作用域覆盖后的清单，供 Context Broker 做确定性选择。"""
        return tuple(sorted(self._manifests.values(), key=lambda entry: entry.name))


def _read_manifest(path: Path, root: Path, scope: CapabilityScope) -> SkillManifest:
    raw = _read_skill_bytes(path, root)
    metadata, _ = _parse_skill_document(raw, path)
    name = _required_text(metadata, "name", path, 64)
    description = _required_text(metadata, "description", path, 1_024)
    if not _SKILL_NAME_PATTERN.fullmatch(name):
        raise SkillError("INVALID_SKILL_NAME", path, "Skill 名称只能使用小写字母、数字和连字符。")
    if path.parent.name != name:
        raise SkillError("SKILL_DIRECTORY_NAME_MISMATCH", path, "Skill 目录名必须与 name 一致。")
    allowed_tools = _allowed_tools(metadata.get("allowed-tools", metadata.get("allowed_tools")), path)
    user_invocable = _optional_bool(metadata, "user-invocable", "user_invocable", True, path)
    disable_model_invocation = _optional_bool(
        metadata,
        "disable-model-invocation",
        "disable_model_invocation",
        False,
        path,
    )
    compatibility_value = metadata.get("compatibility")
    if compatibility_value is not None and not isinstance(compatibility_value, str):
        raise SkillError("INVALID_SKILL_COMPATIBILITY", path, "compatibility 必须是字符串。")
    compatibility = compatibility_value.strip() if isinstance(compatibility_value, str) else None
    return SkillManifest(
        name=name,
        description=description,
        path=path.resolve(),
        root=root,
        scope=scope,
        allowed_tools=allowed_tools,
        user_invocable=user_invocable,
        disable_model_invocation=disable_model_invocation,
        compatibility=compatibility or None,
        content_sha256=hashlib.sha256(raw).hexdigest(),
    )


def _read_skill_bytes(path: Path, root: Path) -> bytes:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise SkillError("SKILL_PATH_ESCAPE", resolved, "Skill 不能通过链接逃离声明根目录。") from error
    if not resolved.is_file():
        raise SkillError("SKILL_FILE_UNAVAILABLE", resolved, "SKILL.md 不可读取。")
    size = resolved.stat().st_size
    if size > MAX_SKILL_BYTES:
        raise SkillError("SKILL_FILE_TOO_LARGE", resolved, "SKILL.md 超过 128 KiB 安全上限。")
    return resolved.read_bytes()


def _parse_skill_document(raw: bytes, path: Path) -> tuple[dict[str, object], str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SkillError("SKILL_NOT_UTF8", path, "SKILL.md 必须使用 UTF-8。") from error
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise SkillError("SKILL_FRONTMATTER_REQUIRED", path, "SKILL.md 必须以 YAML frontmatter 开头。")
    closing = next((index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
    if closing is None:
        raise SkillError("SKILL_FRONTMATTER_UNCLOSED", path, "YAML frontmatter 缺少结束标记。")
    try:
        frontmatter = "".join(lines[1:closing])
        if any(isinstance(token, (AliasToken, AnchorToken)) for token in yaml.scan(frontmatter)):
            raise SkillError("SKILL_YAML_ALIAS_BLOCKED", path, "Skill frontmatter 禁止 YAML anchor 和 alias。")
        metadata = yaml.safe_load(frontmatter) or {}
    except SkillError:
        raise
    except yaml.YAMLError as error:
        raise SkillError("SKILL_FRONTMATTER_INVALID", path, "YAML frontmatter 格式无效。") from error
    if not isinstance(metadata, dict):
        raise SkillError("SKILL_FRONTMATTER_NOT_OBJECT", path, "YAML frontmatter 必须是对象。")
    return metadata, "".join(lines[closing + 1 :]).strip()


def _required_text(metadata: dict[str, object], key: str, path: Path, max_chars: int) -> str:
    value = metadata.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SkillError("SKILL_REQUIRED_FIELD_MISSING", path, f"Skill 缺少有效的 {key}。")
    selected = value.strip()
    if len(selected) > max_chars:
        raise SkillError("SKILL_FIELD_TOO_LONG", path, f"Skill 的 {key} 超过长度限制。")
    return selected


def _allowed_tools(value: object, path: Path) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        candidates = tuple(item for item in re.split(r"[\s,]+", value.strip()) if item)
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        candidates = tuple(item.strip() for item in value if item.strip())
    else:
        raise SkillError("INVALID_SKILL_ALLOWED_TOOLS", path, "allowed-tools 必须是工具名字符串或字符串列表。")
    if len(candidates) != len(set(candidates)) or any(not _TOOL_NAME_PATTERN.fullmatch(item) for item in candidates):
        raise SkillError("INVALID_SKILL_ALLOWED_TOOLS", path, "allowed-tools 包含重复或非法工具名。")
    return candidates


def _optional_bool(
    metadata: dict[str, object],
    primary: str,
    alias: str,
    default: bool,
    path: Path,
) -> bool:
    value = metadata.get(primary, metadata.get(alias, default))
    if not isinstance(value, bool):
        raise SkillError("INVALID_SKILL_BOOLEAN", path, f"{primary} 必须是布尔值。")
    return value
