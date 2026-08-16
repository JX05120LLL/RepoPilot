"""Worktree 内 Spring Boot 应用的受控启动、健康检查和清理。"""

from __future__ import annotations

import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from repopilot_guard.processes import hidden_process_kwargs
from repopilot_guard.recipes import MavenRecipeCatalog


@dataclass(frozen=True, slots=True)
class ApplicationRuntimeResult:
    status: str
    code: str
    port: int
    health_url: str
    duration_ms: int
    log_path: str
    cleanup_status: str

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status, "code": self.code, "port": self.port,
            "health_url": self.health_url, "duration_ms": self.duration_ms,
            "log_path": self.log_path, "cleanup_status": self.cleanup_status,
        }


class SpringBootApplicationRuntime:
    """固定 Maven argv 启动，健康检查只访问本机回环地址且始终清理子进程。"""

    def __init__(self, *, startup_timeout_seconds: int = 45, health_path: str = "/actuator/health") -> None:
        self._startup_timeout_seconds = startup_timeout_seconds
        self._health_path = health_path

    def run(self, worktree: Path, artifact_directory: Path) -> ApplicationRuntimeResult:
        root = worktree.expanduser().resolve()
        started = time.monotonic()
        port = _reserve_loopback_port()
        health_url = f"http://127.0.0.1:{port}{self._health_path}"
        artifact_directory.mkdir(parents=True, exist_ok=True)
        log = artifact_directory / "application-runtime.log"
        executable = MavenRecipeCatalog._maven_executable(root)
        argv = (executable, "-q", "spring-boot:run", f"-Dspring-boot.run.arguments=--server.port={port}")
        process: subprocess.Popen[str] | None = None
        try:
            with log.open("w", encoding="utf-8", errors="replace") as output:
                process = subprocess.Popen(
                    argv, cwd=root, stdout=output, stderr=subprocess.STDOUT, text=True,
                    **hidden_process_kwargs(),
                )
                deadline = started + self._startup_timeout_seconds
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        return self._result("FAILED", "APPLICATION_EXITED_BEFORE_HEALTH", port, health_url, started, log, "NOT_NEEDED")
                    if _healthy(health_url):
                        return self._result("PASSED", "APPLICATION_HEALTHY", port, health_url, started, log, "PENDING")
                    time.sleep(0.25)
                return self._result("FAILED", "APPLICATION_HEALTH_TIMEOUT", port, health_url, started, log, "PENDING")
        except OSError:
            return self._result("BLOCKED", "APPLICATION_START_UNAVAILABLE", port, health_url, started, log, "NOT_STARTED")
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)

    @staticmethod
    def _result(status: str, code: str, port: int, health_url: str, started: float, log: Path, cleanup_status: str) -> ApplicationRuntimeResult:
        # finally 已同步完成清理；没有遗留进程才能交付 PASSED。
        return ApplicationRuntimeResult(status, code, port, health_url, int((time.monotonic() - started) * 1000), log.name, "CLEANED" if cleanup_status == "PENDING" else cleanup_status)


def _reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _healthy(url: str) -> bool:
    try:
        with urlopen(url, timeout=0.5) as response:  # noqa: S310 - URL is locally constructed.
            return 200 <= response.status < 300
    except (URLError, OSError, TimeoutError):
        return False
