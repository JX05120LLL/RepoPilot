"""Java 平台（RepoPilot Platform）的受控 HTTP 客户端。

Python 引擎在任务结束后，用 service-token 将结果（状态/结论/Diff/验证证据）回写到
Java 平台的任务编排接口。此客户端只负责回写，不承载任何权限决策。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PlatformConfig:
    """Java 平台连接参数；均从环境变量读取，绝不硬编码密钥。"""

    base_url: str
    service_token: str


@dataclass(frozen=True, slots=True)
class TaskResultReport:
    """Python 引擎回写的任务结果。"""

    status: str
    verdict: str | None = None
    result_json: str = "{}"

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "verdict": self.verdict,
            "resultJson": self.result_json,
        }


class PlatformClientError(RuntimeError):
    """回写失败时的稳定错误，携带 HTTP 状态码与脱敏响应。"""

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        super().__init__(f"平台回写失败（HTTP {status_code}）：{body}")


def platform_config_from_environment() -> PlatformConfig:
    return PlatformConfig(
        base_url=os.environ.get("REPOPILOT_PLATFORM_URL", "http://127.0.0.1:8081"),
        service_token=os.environ.get("REPOPILOT_SERVICE_TOKEN", "local-service-token"),
    )


class PlatformClient:
    """Python 引擎 → Java 平台的回写客户端。"""

    def __init__(self, config: PlatformConfig | None = None) -> None:
        self._config = config or platform_config_from_environment()

    def report_task_result(self, task_id: str, report: TaskResultReport) -> dict[str, object]:
        url = f"{self._config.base_url.rstrip('/')}/api/tasks/{task_id}/result"
        body = json.dumps(report.to_dict(), ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Service-Token": self._config.service_token,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise PlatformClientError(error.code, error.read().decode("utf-8", errors="replace")) from error
        except urllib.error.URLError as error:
            raise PlatformClientError(0, str(error.reason)) from error
