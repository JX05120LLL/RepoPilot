"""完全本机控制模式下的受控 argv 命令执行器。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, field_validator

from repopilot_guard.capabilities import CapabilityDescriptor, CapabilityKind, CapabilityRisk, CapabilityScope
from repopilot_guard.permissions import PermissionGrant
from repopilot_guard.processes import hidden_process_kwargs


MAX_SHELL_ARGUMENTS = 64
MAX_SHELL_ARGUMENT_CHARS = 2_048
MAX_SHELL_TIMEOUT_SECONDS = 300
DEFAULT_SHELL_TIMEOUT_SECONDS = 60
DEFAULT_SHELL_OUTPUT_CHARS = 16_000

_SHELL_HOSTS = frozenset(
    {"bash", "cmd", "cmd.exe", "fish", "powershell", "powershell.exe", "pwsh", "sh", "zsh"}
)
_HOST_DANGEROUS_COMMANDS = frozenset(
    {"bcdedit", "cipher", "diskpart", "erase", "format", "rd", "reg", "rmdir", "rm", "shutdown", "unlink"}
)
_GIT_MUTATING_SUBCOMMANDS = frozenset({"add", "apply", "branch", "checkout", "clean", "commit", "merge", "push", "reset", "restore", "switch", "tag"})
_NETWORK_COMMANDS = frozenset({"curl", "npm", "npx", "pip", "pip3", "pnpm", "uv", "wget", "yarn"})
_GIT_NETWORK_SUBCOMMANDS = frozenset({"clone", "fetch", "ls-remote", "pull", "remote", "submodule"})
_READ_ONLY_GIT_SUBCOMMANDS = frozenset({"branch", "diff", "grep", "log", "ls-files", "rev-parse", "show", "status"})
_READ_ONLY_VERSION_COMMANDS = frozenset({"java", "javac", "mvn", "mvnw", "mvnw.cmd", "python", "python3"})
_SAFE_ENVIRONMENT_NAMES = ("COMSPEC", "PATHEXT", "PATH", "SYSTEMROOT", "TEMP", "TMP", "WINDIR")
_SECRET_PATTERN = re.compile(r"(?i)(api[_-]?key|authorization|password|secret|token)(\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+")


class ShellCommandRequest(BaseModel):
    """模型或界面提交的结构化命令；完整权限下允许调用 Shell 解释器。"""

    argv: list[str] = Field(min_length=1, max_length=MAX_SHELL_ARGUMENTS)
    cwd: str = Field(default=".", min_length=1, max_length=1_024)
    timeout_seconds: int = Field(default=DEFAULT_SHELL_TIMEOUT_SECONDS, ge=1, le=MAX_SHELL_TIMEOUT_SECONDS)

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, value: list[str]) -> list[str]:
        for argument in value:
            if not isinstance(argument, str) or not argument or len(argument) > MAX_SHELL_ARGUMENT_CHARS:
                raise ValueError("SHELL_ARGUMENT_INVALID")
            if any(character in argument for character in ("\x00", "\r", "\n")):
                raise ValueError("SHELL_ARGUMENT_INVALID")
        return value

    @field_validator("cwd")
    @classmethod
    def validate_cwd(cls, value: str) -> str:
        if any(character in value for character in ("\x00", "\r", "\n")):
            raise ValueError("SHELL_CWD_INVALID")
        return value


class ShellCommandProposal(BaseModel):
    """计划审批后生成的 Shell 执行草案；空列表表示不需要额外命令。"""

    summary: str = Field(min_length=1, max_length=1_000)
    commands: list[ShellCommandRequest] = Field(default_factory=list, max_length=4)


@dataclass(frozen=True, slots=True)
class ShellExecutionResult:
    """可写入 Evidence 的脱敏命令结果，不保留完整环境变量或未截断输出。"""

    status: str
    code: str
    message: str
    argv: tuple[str, ...]
    argv_sha256: str
    working_directory: str | None
    exit_code: int | None
    duration_ms: int
    stdout_summary: str
    stderr_summary: str
    environment_names: tuple[str, ...]
    process_tree_summary: str

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "argv": list(self.argv),
            "argv_sha256": self.argv_sha256,
            "working_directory": self.working_directory,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "stdout_summary": self.stdout_summary,
            "stderr_summary": self.stderr_summary,
            "environment_names": list(self.environment_names),
            "process_tree_summary": self.process_tree_summary,
        }


@dataclass(frozen=True, slots=True)
class ShellCommandPreview:
    """命令执行前的不可变审阅摘要；不会启动任何子进程。"""

    status: str
    code: str
    message: str
    argv: tuple[str, ...]
    argv_sha256: str
    working_directory: str | None
    timeout_seconds: int
    requires_execution_approval: bool
    requires_risk_approval: bool
    risk_categories: tuple[str, ...]

    @property
    def approval_sha256(self) -> str:
        """覆盖 argv、目录、超时和风险标签，作为执行审批冻结指纹。"""

        payload = {
            "argv_sha256": self.argv_sha256,
            "working_directory": self.working_directory,
            "timeout_seconds": self.timeout_seconds,
            "risk_categories": self.risk_categories,
            "requires_risk_approval": self.requires_risk_approval,
        }
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "argv": list(self.argv),
            "argv_sha256": self.argv_sha256,
            "working_directory": self.working_directory,
            "timeout_seconds": self.timeout_seconds,
            "requires_execution_approval": self.requires_execution_approval,
            "requires_risk_approval": self.requires_risk_approval,
            "risk_categories": list(self.risk_categories),
            "approval_sha256": self.approval_sha256,
        }


def shell_capability() -> CapabilityDescriptor:
    """返回 Shell 的统一 Capability 描述，供任务能力目录和 Policy 复用。"""

    return CapabilityDescriptor(
        capability_id="shell",
        name="shell",
        description="在完全本机控制任务中执行经预览审批的 Shell、网络或 Git 交付 argv 命令。",
        kind=CapabilityKind.BUILTIN_TOOL,
        scope=CapabilityScope.BUNDLED,
        source="repopilot:shell-runtime",
        risks=frozenset({CapabilityRisk.PROCESS, CapabilityRisk.WRITE}),
        requires_approval=True,
    )


class ShellRuntime:
    """执行完全本机任务中经过冻结审批的 argv 命令。

    这不是操作系统级沙箱。启用后，已确认的 FULL_LOCAL 任务可调用 Shell
    解释器、Git commit/push 和项目外路径；每条命令仍需冻结预览、风险审批、
    输出脱敏、超时和可取消的进程树管理。
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        environment: Mapping[str, str] | None = None,
        max_output_chars: int = DEFAULT_SHELL_OUTPUT_CHARS,
    ) -> None:
        self._enabled = enabled
        self._environment = dict(os.environ if environment is None else environment)
        self._max_output_chars = max_output_chars

    @property
    def enabled(self) -> bool:
        """仅用于决定是否向任务暴露该能力；实际调用仍会再次校验开关。"""

        return self._enabled

    def run(
        self,
        workspace_root: Path,
        request: ShellCommandRequest,
        permission: PermissionGrant,
        *,
        capability_approved: bool = False,
        risk_approved: bool = False,
        read_only_only: bool = False,
        cancellation_requested: Callable[[], bool] | None = None,
    ) -> ShellExecutionResult:
        """执行命令或返回稳定的 BLOCKED/FAILED 结果。"""

        digest = _argv_digest(request.argv)
        redacted_argv = tuple(_redact(value) for value in request.argv)
        preview = self.preview(
            workspace_root,
            request,
            permission,
            capability_approved=capability_approved,
            read_only_only=read_only_only,
        )
        if preview.status != "READY":
            return self._blocked(
                preview.code,
                preview.message,
                redacted_argv,
                digest,
                preview.working_directory,
            )

        root = workspace_root.expanduser().resolve()
        working_directory = _resolve_working_directory(root, request.cwd)

        command_decision = self._check_command(
            root,
            working_directory,
            request.argv,
            risk_approved,
            read_only_only,
            permission.is_full_access,
        )
        if command_decision is not None:
            return self._blocked(command_decision[0], command_decision[1], redacted_argv, digest, str(working_directory))

        started = time.monotonic()
        environment = self._sanitized_environment()
        try:
            process = subprocess.Popen(
                request.argv,
                cwd=working_directory,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                **_managed_process_kwargs(),
            )
        except OSError:
            return ShellExecutionResult(
                "BLOCKED", "SHELL_EXECUTABLE_UNAVAILABLE", "命令无法启动，请检查项目依赖或 PATH。", redacted_argv, digest,
                str(working_directory), None, _duration_ms(started), "", "", tuple(sorted(environment)), "NOT_STARTED",
            )

        deadline = started + request.timeout_seconds
        while True:
            if cancellation_requested and cancellation_requested():
                stdout, stderr, tree_summary = _stop_process_tree(process)
                return self._result(
                    "BLOCKED", "SHELL_CANCELLED", "命令已按任务取消请求终止。", redacted_argv, digest, working_directory,
                    process.returncode, started, stdout, stderr, environment, tree_summary,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stdout, stderr, tree_summary = _stop_process_tree(process)
                return self._result(
                    "FAILED", "SHELL_TIMEOUT", "命令超过超时限制，已请求终止进程树。", redacted_argv, digest, working_directory,
                    process.returncode, started, stdout, stderr, environment, tree_summary,
                )
            try:
                stdout, stderr = process.communicate(timeout=min(0.25, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
            except OSError:
                stdout, stderr, tree_summary = _stop_process_tree(process)
                return self._result(
                    "BLOCKED", "SHELL_PROCESS_IO_FAILED", "命令进程通信失败，已请求终止进程树。", redacted_argv, digest, working_directory,
                    process.returncode, started, stdout, stderr, environment, tree_summary,
                )

        status = "READY" if process.returncode == 0 else "FAILED"
        return self._result(
            status,
            "SHELL_SUCCEEDED" if status == "READY" else "SHELL_EXIT_NONZERO",
            "命令执行完成。" if status == "READY" else "命令以非零退出码结束。",
            redacted_argv,
            digest,
            working_directory,
            process.returncode,
            started,
            stdout,
            stderr,
            environment,
            "ROOT_PROCESS_EXITED",
        )

    def preview(
        self,
        workspace_root: Path,
        request: ShellCommandRequest,
        permission: PermissionGrant,
        *,
        capability_approved: bool = False,
        read_only_only: bool = False,
    ) -> ShellCommandPreview:
        """校验并渲染命令预览，绝不创建进程或读取命令输出。"""

        digest = _argv_digest(request.argv)
        redacted_argv = tuple(_redact(value) for value in request.argv)
        if not self._enabled:
            return self._preview_blocked(
                "SHELL_FEATURE_DISABLED", "本机 Shell Runtime 未启用。", redacted_argv, digest, request.timeout_seconds
            )
        if not permission.is_full_access:
            return self._preview_blocked(
                "SHELL_FULL_LOCAL_REQUIRED", "Shell 仅允许已确认的完全本机控制任务。", redacted_argv, digest, request.timeout_seconds
            )
        if not capability_approved:
            return self._preview_blocked(
                "SHELL_CAPABILITY_APPROVAL_REQUIRED", "Shell 需要任务级能力审批。", redacted_argv, digest, request.timeout_seconds
            )

        root = workspace_root.expanduser().resolve()
        if not root.is_dir():
            return self._preview_blocked(
                "SHELL_WORKSPACE_UNAVAILABLE", "任务工作目录不存在或不可读取。", redacted_argv, digest, request.timeout_seconds
            )
        working_directory = _resolve_working_directory(root, request.cwd)
        if not working_directory.is_dir():
            return self._preview_blocked(
                "SHELL_WORKING_DIRECTORY_UNAVAILABLE",
                "命令工作目录不存在或不可读取。",
                redacted_argv,
                digest,
                request.timeout_seconds,
                str(working_directory),
            )
        # 预览允许标出网络等二次风险，但不会因此执行命令。
        command_decision = self._check_command(
            root,
            working_directory,
            request.argv,
            risk_approved=True,
            read_only_only=read_only_only,
            full_local=permission.is_full_access,
        )
        if command_decision is not None:
            return self._preview_blocked(
                command_decision[0],
                command_decision[1],
                redacted_argv,
                digest,
                request.timeout_seconds,
                str(working_directory),
            )
        risks = _command_risk_categories(request.argv)
        return ShellCommandPreview(
            "READY",
            "SHELL_PREVIEW_READY",
            "命令已完成预校验，执行前仍需要任务执行审批。",
            redacted_argv,
            digest,
            str(working_directory),
            request.timeout_seconds,
            True,
            any(category not in {"process", "read"} for category in risks),
            risks,
        )

    def as_structured_tool(
        self,
        workspace_root: Path,
        permission: PermissionGrant,
        *,
        capability_approved: bool,
        risk_approved: bool = False,
        read_only_only: bool = False,
        cancellation_requested: Callable[[], bool] | None = None,
    ) -> StructuredTool:
        """将 Shell 封装为显式 Structured Tool，交由 ToolRuntime 统一登记和调用。"""

        def execute(argv: list[str], cwd: str = ".", timeout_seconds: int = DEFAULT_SHELL_TIMEOUT_SECONDS) -> dict[str, object]:
            try:
                request = ShellCommandRequest(argv=argv, cwd=cwd, timeout_seconds=timeout_seconds)
            except ValueError:
                raw_argv = tuple(str(item) for item in argv)
                return self._blocked(
                    "SHELL_REQUEST_INVALID",
                    "Shell 命令参数不符合受控 argv 契约。",
                    tuple(_redact(item) for item in raw_argv),
                    _argv_digest(raw_argv),
                ).to_dict()
            preview = self.preview(
                workspace_root,
                request,
                permission,
                capability_approved=capability_approved,
                read_only_only=read_only_only,
            )
            if preview.status != "READY":
                return {
                    "status": preview.status,
                    "code": preview.code,
                    "message": preview.message,
                    "preview": preview.to_dict(),
                }
            # 非只读命令必须由后续执行阶段持有明确审批后再调用底层 run，
            # 研究 Tool 永远不能用一次模型调用同时完成“预览 + 写入”。
            if not read_only_only:
                return {
                    "status": "BLOCKED",
                    "code": "SHELL_EXECUTION_APPROVAL_REQUIRED",
                    "message": "命令已完成预览，等待执行审批后才能启动。",
                    "preview": preview.to_dict(),
                }
            result = self.run(
                workspace_root,
                request,
                permission,
                capability_approved=capability_approved,
                risk_approved=risk_approved,
                read_only_only=read_only_only,
                cancellation_requested=cancellation_requested,
            ).to_dict()
            result["preview"] = preview.to_dict()
            return result

        return StructuredTool.from_function(
            execute,
            name="shell",
            description="在完全本机控制任务中执行已审批的 argv 命令；研究阶段仍只允许只读命令。",
        )

    def _check_command(
        self,
        root: Path,
        working_directory: Path,
        argv: list[str],
        risk_approved: bool,
        read_only_only: bool,
        full_local: bool,
    ) -> tuple[str, str] | None:
        executable = Path(argv[0]).name.lower()
        command_name = executable.removesuffix(".exe")
        git_subcommand = next((argument.lower() for argument in argv[1:] if not argument.startswith("-")), "")
        risks = _command_risk_categories(argv)
        if (command_name in _NETWORK_COMMANDS or (command_name == "git" and git_subcommand in _GIT_NETWORK_SUBCOMMANDS)) and not risk_approved:
            return "SHELL_NETWORK_APPROVAL_REQUIRED", "网络或包管理命令需要额外能力审批。"
        if read_only_only and not _is_read_only_command(command_name, argv[1:]):
            return "SHELL_EXECUTION_APPROVAL_REQUIRED", "研究阶段仅允许已识别的只读项目命令。"
        if any(category not in {"process", "read"} for category in risks) and not risk_approved:
            return "SHELL_RISK_APPROVAL_REQUIRED", "完全本机高风险命令需要单独冻结审批。"
        if not full_local:
            return "SHELL_FULL_LOCAL_REQUIRED", "Shell 仅允许已确认的完全本机控制任务。"
        return None

    def _sanitized_environment(self) -> dict[str, str]:
        """只继承启动常见工具所需的基础变量，避免把 API Key 等传给子进程。"""

        return {
            name: self._environment[name]
            for name in _SAFE_ENVIRONMENT_NAMES
            if self._environment.get(name)
        }

    def _result(
        self,
        status: str,
        code: str,
        message: str,
        argv: tuple[str, ...],
        digest: str,
        working_directory: Path,
        exit_code: int | None,
        started: float,
        stdout: str,
        stderr: str,
        environment: dict[str, str],
        process_tree_summary: str,
    ) -> ShellExecutionResult:
        return ShellExecutionResult(
            status,
            code,
            message,
            argv,
            digest,
            str(working_directory),
            exit_code,
            _duration_ms(started),
            _truncate(_redact(stdout), self._max_output_chars),
            _truncate(_redact(stderr), self._max_output_chars),
            tuple(sorted(environment)),
            process_tree_summary,
        )

    @staticmethod
    def _preview_blocked(
        code: str,
        message: str,
        argv: tuple[str, ...],
        digest: str,
        timeout_seconds: int,
        working_directory: str | None = None,
    ) -> ShellCommandPreview:
        return ShellCommandPreview(
            "BLOCKED",
            code,
            message,
            argv,
            digest,
            working_directory,
            timeout_seconds,
            False,
            False,
            (),
        )

    @staticmethod
    def _blocked(
        code: str,
        message: str,
        argv: tuple[str, ...],
        digest: str,
        working_directory: str | None = None,
    ) -> ShellExecutionResult:
        return ShellExecutionResult(
            "BLOCKED", code, message, argv, digest, working_directory, None, 0, "", "", (), "NOT_STARTED"
        )


def _managed_process_kwargs() -> dict[str, object]:
    """创建独立进程组，取消/超时时可清理该命令启动的子进程树。"""

    options = dict(hidden_process_kwargs())
    if os.name == "nt":
        options["creationflags"] = int(options.get("creationflags", 0)) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        options["start_new_session"] = True
    return options


def _stop_process_tree(process: subprocess.Popen[str]) -> tuple[str, str, str]:
    """只清理 RepoPilot 自己启动的独立进程组。"""

    if process.poll() is not None:
        stdout, stderr = process.communicate()
        return stdout, stderr, "ROOT_PROCESS_EXITED"
    if os.name == "nt":
        try:
            completed = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
                **hidden_process_kwargs(),
            )
            tree_summary = "PROCESS_TREE_TERMINATED" if completed.returncode == 0 else "PROCESS_TREE_TERMINATION_FALLBACK"
        except (OSError, subprocess.TimeoutExpired):
            tree_summary = "PROCESS_TREE_TERMINATION_FALLBACK"
        _kill_if_running(process)
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            tree_summary = "PROCESS_GROUP_TERMINATED"
        except (OSError, ProcessLookupError):
            tree_summary = "PROCESS_TREE_TERMINATION_FALLBACK"
        _kill_if_running(process)
    try:
        stdout, stderr = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate(timeout=5)
    return stdout, stderr, tree_summary


def _kill_if_running(process: subprocess.Popen[str]) -> None:
    """避免 Windows taskkill 与 Popen.kill 之间的正常退出竞态变成运行时异常。"""

    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass


def _argument_stays_within_project(root: Path, working_directory: Path, argument: str) -> bool:
    """拒绝显式绝对路径与 ``..`` 路径逃逸；普通参数不被误当成路径。"""

    normalized = argument.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:[/\\]", argument):
        return _is_within(root, Path(argument).expanduser().resolve())
    if normalized == ".." or normalized.startswith("../") or "/../" in normalized:
        return _is_within(root, (working_directory / argument).resolve())
    return True


def _resolve_working_directory(root: Path, requested: str) -> Path:
    """完整权限允许绝对工作目录；相对目录仍相对当前项目解析。"""

    candidate = Path(requested).expanduser()
    return candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _is_read_only_command(executable: str, arguments: list[str]) -> bool:
    """研究循环只接受能确定为只读的命令子集，未知命令必须进入执行审批。"""

    if executable == "git":
        subcommand = next((item.lower() for item in arguments if not item.startswith("-")), "")
        if subcommand not in _READ_ONLY_GIT_SUBCOMMANDS:
            return False
        if subcommand == "branch":
            return "--show-current" in arguments or "--list" in arguments
        return True
    if executable in _READ_ONLY_VERSION_COMMANDS:
        return arguments in (["--version"], ["-version"], ["-v"])
    return False


def _command_risk_categories(argv: list[str]) -> tuple[str, ...]:
    """以稳定、可展示的标签描述执行风险，不把模型文本当作风险判断来源。"""

    executable = Path(argv[0]).name.lower().removesuffix(".exe")
    categories = {"process", "write"}
    git_subcommand = next((argument.lower() for argument in argv[1:] if not argument.startswith("-")), "")
    if executable in _SHELL_HOSTS:
        categories.add("shell_interpreter")
    if executable in _HOST_DANGEROUS_COMMANDS:
        categories.add("host_operation")
    if executable == "git" and git_subcommand in _GIT_MUTATING_SUBCOMMANDS:
        categories.add("git_mutation")
    if executable == "git" and git_subcommand == "commit":
        categories.add("git_commit")
    if executable == "git" and git_subcommand == "push":
        categories.add("git_push")
    if executable in _NETWORK_COMMANDS or (executable == "git" and git_subcommand in _GIT_NETWORK_SUBCOMMANDS):
        categories.add("network")
    if _is_read_only_command(executable, argv[1:]):
        categories.discard("write")
        categories.add("read")
    return tuple(sorted(categories))


def _argv_digest(argv: list[str]) -> str:
    return hashlib.sha256("\0".join(argv).encode("utf-8")).hexdigest()


def _redact(value: str) -> str:
    return _SECRET_PATTERN.sub(r"\1\2[REDACTED]", value)


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit] + "\n...[已截断]"


def _duration_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
