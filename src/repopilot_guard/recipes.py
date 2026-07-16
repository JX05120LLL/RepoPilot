"""构建经校验的 Maven 命令计划，不暴露任意 Shell 入口。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from repopilot_guard.policy import MavenRecipeName, PolicyGuard


@dataclass(frozen=True, slots=True)
class RecipeCommand:
    recipe: MavenRecipeName
    argv: tuple[str, ...]
    working_directory: Path


class MavenRecipeCatalog:
    """构建 MVP 允许的、范围小且可审计的命令集合。"""

    def build(
        self,
        repository: Path,
        recipe: MavenRecipeName,
        test_class: str | None = None,
    ) -> RecipeCommand:
        repository = repository.expanduser().resolve()
        decision = PolicyGuard(repository).check_recipe(recipe, test_class)
        if not decision.allowed:
            raise ValueError(decision.reason)

        executable = self._maven_executable(repository)
        arguments = ["-q"]
        if recipe is MavenRecipeName.COMPILE:
            arguments.extend(["-DskipTests", "compile"])
        elif recipe is MavenRecipeName.TEST:
            arguments.append("test")
        else:
            arguments.extend([f"-Dtest={test_class}", "test"])

        return RecipeCommand(recipe, tuple([executable, *arguments]), repository)

    @staticmethod
    def _maven_executable(repository: Path) -> str:
        windows_wrapper = repository / "mvnw.cmd"
        unix_wrapper = repository / "mvnw"
        if os.name == "nt" and windows_wrapper.is_file():
            return str(windows_wrapper)
        if unix_wrapper.is_file():
            return str(unix_wrapper)
        return "mvn"
