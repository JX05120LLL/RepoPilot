"""第一阶段的环境配置与可审计校验。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from repopilot_guard.models import TaskBudget


_LOCAL_ENV_CONFIG = SettingsConfigDict(
    env_file=".env",
    env_file_encoding="utf-8",
    extra="ignore",
    populate_by_name=True,
)


@dataclass(frozen=True)
class ComponentCheck:
    """不携带敏感值的组件检查结果。"""

    component: str
    ready: bool
    code: str
    message: str
    missing_fields: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        return "READY" if self.ready else "BLOCKED"

    def to_dict(self) -> dict[str, object]:
        return {
            "component": self.component,
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "missing_fields": list(self.missing_fields),
        }


class AppSettings(BaseSettings):
    """从环境变量加载服务端配置，密钥始终以 SecretStr 保存。"""

    model_config = _LOCAL_ENV_CONFIG

    chat_base_url: str | None = Field(default=None, validation_alias="REPOPILOT_CHAT_BASE_URL")
    chat_api_key: SecretStr | None = Field(default=None, validation_alias="REPOPILOT_CHAT_API_KEY")
    chat_model: str | None = Field(default=None, validation_alias="REPOPILOT_CHAT_MODEL")
    chat_input_price_per_million: float | None = Field(default=None, ge=0, validation_alias="REPOPILOT_CHAT_INPUT_PRICE_PER_MILLION")
    chat_output_price_per_million: float | None = Field(default=None, ge=0, validation_alias="REPOPILOT_CHAT_OUTPUT_PRICE_PER_MILLION")
    chat_price_currency: str = Field(default="CNY", min_length=1, max_length=8, validation_alias="REPOPILOT_CHAT_PRICE_CURRENCY")
    task_max_total_tokens: int | None = Field(default=None, ge=1, validation_alias="REPOPILOT_TASK_MAX_TOTAL_TOKENS")
    task_max_estimated_cost: float | None = Field(default=None, ge=0, validation_alias="REPOPILOT_TASK_MAX_ESTIMATED_COST")

    embedding_base_url: str | None = Field(default=None, validation_alias="REPOPILOT_EMBEDDING_BASE_URL")
    embedding_api_key: SecretStr | None = Field(default=None, validation_alias="REPOPILOT_EMBEDDING_API_KEY")
    embedding_model: str | None = Field(default=None, validation_alias="REPOPILOT_EMBEDDING_MODEL")
    embedding_dimensions: int | None = Field(default=None, validation_alias="REPOPILOT_EMBEDDING_DIMENSIONS")

    qdrant_url: str = Field(default="http://127.0.0.1:6333", validation_alias="REPOPILOT_QDRANT_URL")
    state_db_path: Path = Field(default=Path(".repopilot/state.sqlite"), validation_alias="REPOPILOT_STATE_DB_PATH")

    def task_budget(self) -> TaskBudget:
        """将本机服务端策略冻结为任务预算；成本上限沿用聊天模型计价币种。"""

        return TaskBudget(
            max_total_tokens=self.task_max_total_tokens,
            max_estimated_cost=self.task_max_estimated_cost,
            currency=self.chat_price_currency if self.task_max_estimated_cost is not None else None,
        )

    def chat_check(self) -> ComponentCheck:
        missing = self._missing(
            {
                "REPOPILOT_CHAT_BASE_URL": self.chat_base_url,
                "REPOPILOT_CHAT_API_KEY": self.chat_api_key,
                "REPOPILOT_CHAT_MODEL": self.chat_model,
            }
        )
        return self._component_check("chat_provider", missing)

    def embedding_check(self) -> ComponentCheck:
        missing = self._missing(
            {
                "REPOPILOT_EMBEDDING_BASE_URL": self.embedding_base_url,
                "REPOPILOT_EMBEDDING_API_KEY": self.embedding_api_key,
                "REPOPILOT_EMBEDDING_MODEL": self.embedding_model,
                "REPOPILOT_EMBEDDING_DIMENSIONS": self.embedding_dimensions,
            }
        )
        if self.embedding_dimensions is not None and self.embedding_dimensions <= 0:
            return ComponentCheck(
                component="embedding_provider",
                ready=False,
                code="INVALID_EMBEDDING_DIMENSIONS",
                message="REPOPILOT_EMBEDDING_DIMENSIONS 必须是正整数。",
            )
        return self._component_check("embedding_provider", missing)

    def qdrant_settings_check(self) -> ComponentCheck:
        missing = self._missing({"REPOPILOT_QDRANT_URL": self.qdrant_url})
        return self._component_check("qdrant_settings", missing)

    def qdrant_bootstrap_check(self) -> ComponentCheck:
        """Qdrant 初始化只依赖地址和向量维度，不需要模型密钥。"""

        missing = self._missing(
            {
                "REPOPILOT_QDRANT_URL": self.qdrant_url,
                "REPOPILOT_EMBEDDING_DIMENSIONS": self.embedding_dimensions,
            }
        )
        if self.embedding_dimensions is not None and self.embedding_dimensions <= 0:
            return ComponentCheck(
                component="qdrant_bootstrap",
                ready=False,
                code="INVALID_EMBEDDING_DIMENSIONS",
                message="REPOPILOT_EMBEDDING_DIMENSIONS 必须是正整数。",
            )
        return self._component_check("qdrant_bootstrap", missing)

    @staticmethod
    def _missing(values: dict[str, object | None]) -> tuple[str, ...]:
        missing: list[str] = []
        for name, value in values.items():
            if value is None or value == "":
                missing.append(name)
            elif isinstance(value, SecretStr) and not value.get_secret_value().strip():
                missing.append(name)
        return tuple(missing)

    @staticmethod
    def _component_check(component: str, missing: tuple[str, ...]) -> ComponentCheck:
        if missing:
            return ComponentCheck(
                component=component,
                ready=False,
                code="MISSING_CONFIGURATION",
                message="缺少必填配置，未执行外部调用。",
                missing_fields=missing,
            )
        return ComponentCheck(
            component=component,
            ready=True,
            code="CONFIGURATION_READY",
            message="配置完整；第一阶段不会发起模型调用。",
        )


class LocalStateSettings(BaseSettings):
    """只解析本机 SQLite 路径，避免管理命令被模型配置错误阻断。"""

    model_config = _LOCAL_ENV_CONFIG
    state_db_path: Path = Field(default=Path(".repopilot/state.sqlite"), validation_alias="REPOPILOT_STATE_DB_PATH")


def sanitized_settings_error() -> ComponentCheck:
    """将 Pydantic 配置错误转换为不含原始输入的审计结果。"""

    return ComponentCheck(
        component="settings",
        ready=False,
        code="INVALID_CONFIGURATION",
        message="环境变量格式不正确，请检查配置名称和数值格式。",
    )
