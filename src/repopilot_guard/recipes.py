"""构建经校验的 Maven 命令计划，不暴露任意 Shell 入口。"""

from __future__ import annotations

import os
import json
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from repopilot_guard.permissions import PermissionGrant
from repopilot_guard.policy import GradleRecipeName, MavenRecipeName, NodeRecipeName, PolicyGuard, PytestRecipeName
from repopilot_guard.processes import hidden_process_kwargs


@dataclass(frozen=True, slots=True)
class RecipeCommand:
    recipe: MavenRecipeName | GradleRecipeName | PytestRecipeName | NodeRecipeName
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
                **hidden_process_kwargs(),
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


@dataclass(frozen=True, slots=True)
class GradleExecutionResult:
    """固定 Gradle 配方的可审计结果；字段与 Maven 结果保持可投影性。"""

    status: str
    code: str
    recipe: GradleRecipeName
    argv: tuple[str, ...]
    exit_code: int | None
    duration_ms: int
    stdout_summary: str
    stderr_summary: str
    test_reports: tuple[str, ...]


class GradleRecipeCatalog:
    """Gradle 只开放编译、全量测试和目标测试三种直接 argv 配方。"""

    def build(
        self,
        repository: Path,
        recipe: GradleRecipeName,
        test_class: str | None = None,
        permission: PermissionGrant | None = None,
    ) -> RecipeCommand:
        repository = repository.expanduser().resolve()
        decision = PolicyGuard(repository, permission).check_recipe(recipe, test_class)
        if not decision.allowed:
            raise ValueError(decision.reason)
        arguments = ["--no-daemon", "--console=plain", "-q"]
        if recipe is GradleRecipeName.COMPILE:
            arguments.append("classes")
        elif recipe is GradleRecipeName.TEST:
            arguments.append("test")
        else:
            arguments.extend(["test", "--tests", str(test_class)])
        return RecipeCommand(recipe, tuple([self._gradle_executable(repository), *arguments]), repository)

    @staticmethod
    def _gradle_executable(repository: Path) -> str:
        windows_wrapper = repository / "gradlew.bat"
        unix_wrapper = repository / "gradlew"
        if os.name == "nt" and windows_wrapper.is_file():
            return str(windows_wrapper)
        if unix_wrapper.is_file():
            return str(unix_wrapper)
        executable_names = ("gradle.bat", "gradle") if os.name == "nt" else ("gradle",)
        for executable_name in executable_names:
            executable = shutil.which(executable_name)
            if executable:
                return executable
        return "gradle"


class GradleRecipeRunner:
    """执行受控 Gradle argv，不接受 shell 文本、管道或任意任务名称。"""

    def __init__(self, catalog: GradleRecipeCatalog | None = None, timeout_seconds: int = 300, max_output_chars: int = 16_000) -> None:
        self._catalog = catalog or GradleRecipeCatalog()
        self._timeout_seconds = timeout_seconds
        self._max_output_chars = max_output_chars

    def run(
        self,
        repository: Path,
        recipe: GradleRecipeName,
        permission: PermissionGrant,
        test_class: str | None = None,
        cancellation_requested: Callable[[], bool] | None = None,
    ) -> GradleExecutionResult:
        try:
            command = self._catalog.build(repository, recipe, test_class, permission)
        except ValueError as error:
            return GradleExecutionResult("BLOCKED", "GRADLE_RECIPE_BLOCKED", recipe, (), None, 0, "", str(error), ())
        started = time.monotonic()
        try:
            process = subprocess.Popen(command.argv, cwd=command.working_directory, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", **hidden_process_kwargs())
        except OSError as error:
            return GradleExecutionResult("BLOCKED", "GRADLE_UNAVAILABLE", recipe, command.argv, None, _duration_ms(started), "", str(error), ())
        deadline = started + self._timeout_seconds
        while True:
            if cancellation_requested and cancellation_requested():
                stdout, stderr = _stop_process(process)
                return self._result("BLOCKED", "GRADLE_CANCELLED", recipe, command, process.returncode, started, stdout, stderr)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stdout, stderr = _stop_process(process)
                return self._result("FAILED", "GRADLE_TIMEOUT", recipe, command, None, started, stdout, stderr)
            try:
                stdout, stderr = process.communicate(timeout=min(0.25, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        return self._result(
            "PASSED" if process.returncode == 0 else "FAILED",
            "GRADLE_SUCCEEDED" if process.returncode == 0 else "GRADLE_FAILED",
            recipe,
            command,
            process.returncode,
            started,
            stdout,
            stderr,
        )

    def _result(self, status: str, code: str, recipe: GradleRecipeName, command: RecipeCommand, exit_code: int | None, started: float, stdout: str, stderr: str) -> GradleExecutionResult:
        return GradleExecutionResult(status, code, recipe, command.argv, exit_code, _duration_ms(started), _truncate(stdout, self._max_output_chars), _truncate(stderr, self._max_output_chars), self._test_reports(command.working_directory))

    @staticmethod
    def _test_reports(repository: Path) -> tuple[str, ...]:
        reports = repository / "build" / "test-results" / "test"
        if not reports.is_dir():
            return ()
        return tuple(sorted(path.relative_to(repository).as_posix() for path in reports.iterdir() if path.is_file())[:50])


@dataclass(frozen=True, slots=True)
class PytestExecutionResult:
    """固定 pytest 配方的可审计执行结果，不允许传入任意 pytest 参数。"""

    status: str
    code: str
    recipe: PytestRecipeName
    argv: tuple[str, ...]
    exit_code: int | None
    duration_ms: int
    stdout_summary: str
    stderr_summary: str
    test_reports: tuple[str, ...]


class PytestRecipeCatalog:
    """通过当前受控 Python 运行 pytest，避免依赖 PATH 上的不确定可执行文件。"""

    def build(
        self,
        repository: Path,
        recipe: PytestRecipeName,
        test_class: str | None = None,
        permission: PermissionGrant | None = None,
    ) -> RecipeCommand:
        repository = repository.expanduser().resolve()
        decision = PolicyGuard(repository, permission).check_recipe(recipe, test_class)
        if not decision.allowed:
            raise ValueError(decision.reason)
        arguments = ["-m", "pytest", "-q"]
        if recipe is PytestRecipeName.TARGETED_TEST:
            arguments.append(str(test_class))
        return RecipeCommand(recipe, tuple([sys.executable, *arguments]), repository)


class PytestRecipeRunner:
    """执行固定 pytest argv，超时、取消和输出截断规则与其他 Build Profile 一致。"""

    def __init__(self, catalog: PytestRecipeCatalog | None = None, timeout_seconds: int = 300, max_output_chars: int = 16_000) -> None:
        self._catalog = catalog or PytestRecipeCatalog()
        self._timeout_seconds = timeout_seconds
        self._max_output_chars = max_output_chars

    def run(
        self,
        repository: Path,
        recipe: PytestRecipeName,
        permission: PermissionGrant,
        test_class: str | None = None,
        cancellation_requested: Callable[[], bool] | None = None,
    ) -> PytestExecutionResult:
        try:
            command = self._catalog.build(repository, recipe, test_class, permission)
        except ValueError as error:
            return PytestExecutionResult("BLOCKED", "PYTEST_RECIPE_BLOCKED", recipe, (), None, 0, "", str(error), ())
        started = time.monotonic()
        try:
            process = subprocess.Popen(command.argv, cwd=command.working_directory, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", **hidden_process_kwargs())
        except OSError as error:
            return PytestExecutionResult("BLOCKED", "PYTEST_UNAVAILABLE", recipe, command.argv, None, _duration_ms(started), "", str(error), ())
        deadline = started + self._timeout_seconds
        while True:
            if cancellation_requested and cancellation_requested():
                stdout, stderr = _stop_process(process)
                return self._result("BLOCKED", "PYTEST_CANCELLED", recipe, command, process.returncode, started, stdout, stderr)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stdout, stderr = _stop_process(process)
                return self._result("FAILED", "PYTEST_TIMEOUT", recipe, command, None, started, stdout, stderr)
            try:
                stdout, stderr = process.communicate(timeout=min(0.25, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        return self._result(
            "PASSED" if process.returncode == 0 else "FAILED",
            "PYTEST_SUCCEEDED" if process.returncode == 0 else "PYTEST_FAILED",
            recipe,
            command,
            process.returncode,
            started,
            stdout,
            stderr,
        )

    def _result(self, status: str, code: str, recipe: PytestRecipeName, command: RecipeCommand, exit_code: int | None, started: float, stdout: str, stderr: str) -> PytestExecutionResult:
        return PytestExecutionResult(status, code, recipe, command.argv, exit_code, _duration_ms(started), _truncate(stdout, self._max_output_chars), _truncate(stderr, self._max_output_chars), ())


@dataclass(frozen=True, slots=True)
class NodeExecutionResult:
    """固定 npm/pnpm test Recipe 的可审计结果，不接受模型提供的脚本名或参数。"""

    status: str
    code: str
    recipe: NodeRecipeName
    argv: tuple[str, ...]
    exit_code: int | None
    duration_ms: int
    stdout_summary: str
    stderr_summary: str
    test_reports: tuple[str, ...]


class NodeRecipeCatalog:
    """仅执行 package.json 中名为 test 的脚本；不开放 npm exec、install 或任意 run。"""

    def __init__(self, command_lookup: Callable[[str], str | None] | None = None) -> None:
        self._command_lookup = command_lookup or shutil.which

    def build(
        self,
        repository: Path,
        recipe: NodeRecipeName,
        test_class: str | None = None,
        permission: PermissionGrant | None = None,
    ) -> RecipeCommand:
        repository = repository.expanduser().resolve()
        decision = PolicyGuard(repository, permission).check_recipe(recipe, test_class)
        if not decision.allowed:
            raise ValueError(decision.reason)
        if not (repository / "package.json").is_file():
            raise ValueError("package.json was not found.")
        try:
            package = json.loads((repository / "package.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("package.json is not readable JSON.") from error
        scripts = package.get("scripts") if isinstance(package, dict) else None
        if not isinstance(scripts, dict) or not isinstance(scripts.get("test"), str) or not scripts["test"].strip():
            raise ValueError("package.json does not define a non-empty test script.")
        executable = self._package_manager(recipe)
        # `--silent` 必须位于脚本名之前，避免 npm/pnpm 将它透传给用户的 test 脚本。
        return RecipeCommand(recipe, (*executable, "run", "--silent", "test"), repository)

    def _package_manager(self, recipe: NodeRecipeName) -> tuple[str, ...]:
        base_name = "pnpm" if recipe is NodeRecipeName.PNPM_TEST else "npm"
        # Corepack 是 Node 官方的包管理器分发入口。pnpm 的 shim 不可用时仍保持
        # pnpm 语义，绝不把任务静默替换成 npm。
        if recipe is NodeRecipeName.PNPM_TEST:
            corepack_names = ("corepack.cmd", "corepack") if os.name == "nt" else ("corepack",)
            for name in corepack_names:
                executable = self._command_lookup(name)
                if executable:
                    return (executable, "pnpm")
        names = (f"{base_name}.cmd", base_name) if os.name == "nt" else (base_name,)
        for name in names:
            executable = self._command_lookup(name)
            if executable:
                return (executable,)
        return (base_name,)


class NodeRecipeRunner:
    """执行固定 npm/pnpm test argv，并与其他 Build Profile 使用同一超时与输出规则。"""

    def __init__(self, catalog: NodeRecipeCatalog | None = None, timeout_seconds: int = 300, max_output_chars: int = 16_000) -> None:
        self._catalog = catalog or NodeRecipeCatalog()
        self._timeout_seconds = timeout_seconds
        self._max_output_chars = max_output_chars

    def run(
        self,
        repository: Path,
        recipe: NodeRecipeName,
        permission: PermissionGrant,
        test_class: str | None = None,
        cancellation_requested: Callable[[], bool] | None = None,
    ) -> NodeExecutionResult:
        try:
            command = self._catalog.build(repository, recipe, test_class, permission)
        except ValueError as error:
            return NodeExecutionResult("BLOCKED", "NODE_RECIPE_BLOCKED", recipe, (), None, 0, "", str(error), ())
        started = time.monotonic()
        try:
            process = subprocess.Popen(command.argv, cwd=command.working_directory, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", **hidden_process_kwargs())
        except OSError as error:
            return NodeExecutionResult("BLOCKED", "NODE_PACKAGE_MANAGER_UNAVAILABLE", recipe, command.argv, None, _duration_ms(started), "", str(error), ())
        deadline = started + self._timeout_seconds
        while True:
            if cancellation_requested and cancellation_requested():
                stdout, stderr = _stop_process(process)
                return self._result("BLOCKED", "NODE_TEST_CANCELLED", recipe, command, process.returncode, started, stdout, stderr)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stdout, stderr = _stop_process(process)
                return self._result("FAILED", "NODE_TEST_TIMEOUT", recipe, command, None, started, stdout, stderr)
            try:
                stdout, stderr = process.communicate(timeout=min(0.25, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        return self._result(
            "PASSED" if process.returncode == 0 else "FAILED",
            "NODE_TEST_SUCCEEDED" if process.returncode == 0 else "NODE_TEST_FAILED",
            recipe,
            command,
            process.returncode,
            started,
            stdout,
            stderr,
        )

    def _result(self, status: str, code: str, recipe: NodeRecipeName, command: RecipeCommand, exit_code: int | None, started: float, stdout: str, stderr: str) -> NodeExecutionResult:
        return NodeExecutionResult(status, code, recipe, command.argv, exit_code, _duration_ms(started), _truncate(stdout, self._max_output_chars), _truncate(stderr, self._max_output_chars), ())


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
