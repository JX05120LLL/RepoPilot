"""语言 Profile 的本机运行时只读探测。

探测只确认受控 Recipe 可能使用的命令或 Python 模块是否存在；不执行命令、不读取
项目源码，也不返回本机二进制的绝对路径。
"""

from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import Callable


CommandLookup = Callable[[str], str | None]
ModuleLookup = Callable[[str], object | None]


@dataclass(frozen=True, slots=True)
class ProfileRuntimeReadiness:
    """某个已实现 Profile 在当前机器上的运行时可用性。"""

    status: str
    code: str
    command: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "status": self.status,
            "code": self.code,
            "command": self.command,
            "message": self.message,
        }


class ProfileRuntimeInspector:
    """基于固定白名单探测 Profile 运行时，不执行任何外部进程。"""

    def __init__(
        self,
        command_lookup: CommandLookup | None = None,
        module_lookup: ModuleLookup | None = None,
    ) -> None:
        self._command_lookup = command_lookup or which
        self._module_lookup = module_lookup or importlib.util.find_spec

    def inspect(self, profile_id: str, repository: Path) -> ProfileRuntimeReadiness:
        root = repository.expanduser().resolve()
        if profile_id == "java_maven":
            return self._command_or_wrapper(
                root,
                "maven",
                ("mvnw.cmd", "mvnw") if os.name == "nt" else ("mvnw", "mvnw.cmd"),
                ("mvn.cmd", "mvn") if os.name == "nt" else ("mvn",),
            )
        if profile_id == "java_gradle":
            return self._command_or_wrapper(
                root,
                "gradle",
                ("gradlew.bat", "gradlew") if os.name == "nt" else ("gradlew", "gradlew.bat"),
                ("gradle.bat", "gradle") if os.name == "nt" else ("gradle",),
            )
        if profile_id == "python_pytest":
            return self._python_pytest()
        if profile_id in {"node_npm", "node_pnpm"}:
            if profile_id == "node_pnpm":
                corepack_candidates = ("corepack.cmd", "corepack") if os.name == "nt" else ("corepack",)
                if any(self._command_lookup(candidate) for candidate in corepack_candidates):
                    return ProfileRuntimeReadiness(
                        "READY",
                        "PNPM_COREPACK_READY",
                        "corepack pnpm",
                        "已检测到 Corepack；pnpm 将通过固定 Corepack argv 调用，不会降级为 npm。",
                    )
            command = "pnpm" if profile_id == "node_pnpm" else "npm"
            candidates = (f"{command}.cmd", command) if os.name == "nt" else (command,)
            return self._command(command, candidates)
        return ProfileRuntimeReadiness(
            "BLOCKED",
            "PROFILE_RUNTIME_UNKNOWN",
            "unknown",
            "当前 Profile 没有受控运行时探测规则，不能声明可执行。",
        )

    def _command_or_wrapper(
        self,
        root: Path,
        command: str,
        wrappers: tuple[str, ...],
        candidates: tuple[str, ...],
    ) -> ProfileRuntimeReadiness:
        if any((root / wrapper).is_file() for wrapper in wrappers):
            return ProfileRuntimeReadiness(
                "READY",
                f"{command.upper()}_WRAPPER_READY",
                command,
                f"已检测到项目 {command} Wrapper；将仅通过固定 Recipe 调用。",
            )
        return self._command(command, candidates)

    def _command(self, command: str, candidates: tuple[str, ...]) -> ProfileRuntimeReadiness:
        if any(self._command_lookup(candidate) for candidate in candidates):
            return ProfileRuntimeReadiness(
                "READY",
                f"{command.upper()}_RUNTIME_READY",
                command,
                f"已检测到本机 {command} 运行时；实际执行仍受固定 Recipe、审批和超时限制。",
            )
        return ProfileRuntimeReadiness(
            "BLOCKED",
            f"{command.upper()}_RUNTIME_UNAVAILABLE",
            command,
            f"未检测到本机 {command} 运行时；RepoPilot 不会自动安装或替换为其他包管理器。",
        )

    def _python_pytest(self) -> ProfileRuntimeReadiness:
        if self._module_lookup("pytest") is not None:
            return ProfileRuntimeReadiness(
                "READY",
                "PYTEST_RUNTIME_READY",
                "python -m pytest",
                "当前 RepoPilot Python 环境已具备 pytest；实际执行仍受固定 Recipe、审批和超时限制。",
            )
        return ProfileRuntimeReadiness(
            "BLOCKED",
            "PYTEST_RUNTIME_UNAVAILABLE",
            "python -m pytest",
            "当前 RepoPilot Python 环境未安装 pytest；不会自动安装依赖。",
        )
