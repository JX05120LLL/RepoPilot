"""在 Agent 工具执行前运行的安全校验。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class ToolName(str, Enum):
    LIST_FILES = "list_files"
    SEARCH_CODE = "search_code"
    READ_FILE = "read_file"
    INSPECT_BUILD = "inspect_build"
    APPLY_PATCH = "apply_patch"
    RUN_RECIPE = "run_recipe"
    GIT_DIFF = "git_diff"
    WRITE_REPORT = "write_report"


class MavenRecipeName(str, Enum):
    COMPILE = "compile"
    TEST = "test"
    TARGETED_TEST = "targeted_test"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    reason: str


class PolicyGuard:
    """强制执行工作区边界和首版工具白名单。"""

    _protected_names = frozenset(
        {
            ".env",
            ".env.local",
            ".env.production",
            "id_rsa",
            "id_ed25519",
            "credentials",
            "credentials.json",
        }
    )
    _protected_directories = frozenset({".git", ".aws", ".ssh", ".gnupg"})
    _protected_suffixes = frozenset({".pem", ".key", ".p12", ".pfx", ".jks"})
    _test_class_pattern = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)*$")

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.expanduser().resolve()

    def check_path(self, tool: ToolName, requested_path: Path) -> PolicyDecision:
        target = requested_path.expanduser().resolve()
        try:
            relative = target.relative_to(self.workspace_root)
        except ValueError:
            return PolicyDecision(False, "Path escapes the isolated workspace.")

        parts = {part.lower() for part in relative.parts}
        filename = target.name.lower()
        if parts.intersection(self._protected_directories):
            return PolicyDecision(False, "Path is inside a protected directory.")
        if filename in self._protected_names:
            return PolicyDecision(False, "Path is a protected secret file.")
        if target.suffix.lower() in self._protected_suffixes:
            return PolicyDecision(False, "Path has a protected secret suffix.")
        if filename.startswith("application-prod"):
            return PolicyDecision(False, "Production configuration is protected.")

        if not isinstance(tool, ToolName):
            return PolicyDecision(False, "Tool is not allowlisted.")
        return PolicyDecision(True, "Path is within the workspace and passes protection rules.")

    def check_recipe(self, recipe: MavenRecipeName, test_class: str | None = None) -> PolicyDecision:
        if recipe is MavenRecipeName.TARGETED_TEST:
            if not test_class:
                return PolicyDecision(False, "targeted_test requires a test class.")
            if not self._test_class_pattern.fullmatch(test_class):
                return PolicyDecision(False, "Test class contains unsupported characters.")
        elif test_class is not None:
            return PolicyDecision(False, "Only targeted_test accepts a test class.")
        return PolicyDecision(True, "Maven Recipe is allowlisted.")
