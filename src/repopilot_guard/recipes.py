"""构建经校验的 Maven 命令计划，不暴露任意 Shell 入口。"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from repopilot_guard.permissions import PermissionGrant
from repopilot_guard.policy import MavenRecipeName, PolicyGuard


@dataclass(frozen=True, slots=True)
class RecipeCommand:
    recipe: MavenRecipeName
    argv: tuple[str, ...]
    working_directory: Path


@dataclass(frozen=True, slots=True)
class MavenExecutionResult:
    """固定 Maven 配方的可审计执行结果，不保存完整构建输出。"""

    status: str
    code: str
    recipe: MavenRecipeName
    argv: tuple[str, ...]
    exit_code: int | None
    duration_ms: int
    stdout_summary: str
    stderr_summary: str
    surefire_reports: tuple[str, ...]


class MavenRecipeCatalog:
    """构建 MVP 允许的、范围小且可审计的命令集合。"""

    def build(
        self,
        repository: Path,
        recipe: MavenRecipeName,
        test_class: str | None = None,
        permission: PermissionGrant | None = None,
    ) -> RecipeCommand:
        repository = repository.expanduser().resolve()
        decision = PolicyGuard(repository, permission).check_recipe(recipe, test_class)
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
        executable_names = ("mvn.cmd", "mvn") if os.name == "nt" else ("mvn",)
        for executable_name in executable_names:
            executable = shutil.which(executable_name)
            if executable:
                return executable
        return "mvn"


class MavenRecipeRunner:
    """只执行白名单 Maven 配方；不会拼接或交给 Shell 解释命令。"""

    def __init__(self, catalog: MavenRecipeCatalog | None = None, timeout_seconds: int = 300, max_output_chars: int = 16_000) -> None:
        self._catalog = catalog or MavenRecipeCatalog()
        self._timeout_seconds = timeout_seconds
        self._max_output_chars = max_output_chars

    def run(
        self,
        repository: Path,
        recipe: MavenRecipeName,
        permission: PermissionGrant,
        test_class: str | None = None,
        cancellation_requested: Callable[[], bool] | None = None,
    ) -> MavenExecutionResult:
        try:
            command = self._catalog.build(repository, recipe, test_class, permission)
        except ValueError as error:
            return MavenExecutionResult("BLOCKED", "MAVEN_RECIPE_BLOCKED", recipe, (), None, 0, "", str(error), ())

        started = time.monotonic()
        try:
            process = subprocess.Popen(
                command.argv,
                cwd=command.working_directory,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as error:
            return MavenExecutionResult(
                "BLOCKED", "MAVEN_UNAVAILABLE", recipe, command.argv, None, _duration_ms(started), "", str(error), (),
            )

        deadline = started + self._timeout_seconds
        while True:
            if cancellation_requested and cancellation_requested():
                stdout, stderr = _stop_process(process)
                return MavenExecutionResult(
                    "BLOCKED", "MAVEN_CANCELLED", recipe, command.argv, process.returncode, _duration_ms(started),
                    _truncate(stdout, self._max_output_chars), _truncate(stderr, self._max_output_chars),
                    self._surefire_reports(command.working_directory),
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stdout, stderr = _stop_process(process)
                return MavenExecutionResult(
                    "FAILED", "MAVEN_TIMEOUT", recipe, command.argv, None, _duration_ms(started),
                    _truncate(stdout, self._max_output_chars), _truncate(stderr, self._max_output_chars),
                    self._surefire_reports(command.working_directory),
                )
            try:
                stdout, stderr = process.communicate(timeout=min(0.25, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
            except OSError as error:
                stdout, stderr = _stop_process(process)
                return MavenExecutionResult(
                    "BLOCKED", "MAVEN_UNAVAILABLE", recipe, command.argv, process.returncode, _duration_ms(started),
                    _truncate(stdout, self._max_output_chars), _truncate(f"{stderr}\n{error}", self._max_output_chars),
                    self._surefire_reports(command.working_directory),
                )

        status = "PASSED" if process.returncode == 0 else "FAILED"
        return MavenExecutionResult(
            status,
            "MAVEN_SUCCEEDED" if status == "PASSED" else "MAVEN_FAILED",
            recipe,
            command.argv,
            process.returncode,
            _duration_ms(started),
            _truncate(stdout, self._max_output_chars),
            _truncate(stderr, self._max_output_chars),
            self._surefire_reports(command.working_directory),
        )

    @staticmethod
    def _surefire_reports(repository: Path) -> tuple[str, ...]:
        reports = repository / "target" / "surefire-reports"
        if not reports.is_dir():
            return ()
        return tuple(sorted(path.relative_to(repository).as_posix() for path in reports.iterdir() if path.is_file())[:50])


def _stop_process(process: subprocess.Popen[str]) -> tuple[str, str]:
    """只终止 RepoPilot 自己启动的 Maven 进程，绝不执行任意系统命令。"""

    if process.poll() is None:
        process.terminate()
    try:
        return process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.communicate(timeout=5)


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit] + "\n...[已截断]"


def _duration_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
