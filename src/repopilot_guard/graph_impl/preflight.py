"""阶段四 Step 2：graph.py 拆分出的子模块（由 graph.py 兼容 shim 统一重导出）。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from repopilot_guard.config import AppSettings, ComponentCheck
from repopilot_guard.preflight import PreflightInspector
from repopilot_guard.providers import OpenAICompatibleProvider
from repopilot_guard.qdrant_bootstrap import check_qdrant_health

@dataclass(frozen=True)
class PhaseOnePreflightResult:
    ready: bool
    checks: tuple[ComponentCheck, ...]

    def to_event(self) -> dict[str, object]:
        return {"type": "PREFLIGHT_COMPLETED", "ready": self.ready, "checks": [check.to_dict() for check in self.checks]}


class GraphPreflightChecker(Protocol):
    def check(self, repository: Path) -> PhaseOnePreflightResult: ...


class PhaseOnePreflightChecker:
    """复用仓库预检，并验证模型、Embedding 与 Qdrant。"""

    def __init__(
        self,
        settings: AppSettings,
        preflight_inspector: PreflightInspector | None = None,
        dependency_setup_check: ComponentCheck | None = None,
    ) -> None:
        self._settings = settings
        self._preflight_inspector = preflight_inspector or PreflightInspector()
        self._dependency_setup_check = dependency_setup_check

    def check(self, repository: Path) -> PhaseOnePreflightResult:
        repository_preflight = self._preflight_inspector.inspect(repository)
        repository_check = ComponentCheck(
            component="repository",
            ready=repository_preflight.ready,
            code="REPOSITORY_READY" if repository_preflight.ready else "REPOSITORY_PREFLIGHT_FAILED",
            message="仓库预检通过。" if repository_preflight.ready else "仓库预检失败。",
            missing_fields=repository_preflight.errors,
        )
        provider = OpenAICompatibleProvider(self._settings)
        qdrant_settings = self._settings.qdrant_settings_check()
        checks: list[ComponentCheck] = [repository_check, provider.chat_check(), provider.embedding_check(), qdrant_settings]
        if self._dependency_setup_check is not None:
            checks.append(self._dependency_setup_check)
        if qdrant_settings.ready:
            checks.append(check_qdrant_health(self._settings.qdrant_url))
        return PhaseOnePreflightResult(ready=all(check.ready for check in checks), checks=tuple(checks))
