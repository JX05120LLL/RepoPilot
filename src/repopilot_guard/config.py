"""第一阶段的环境配置与可审计校验。"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from repopilot_guard.models import TaskBudget


_LOCAL_ENV_CONFIG = SettingsConfigDict(
    env_file=".env",
    env_file_encoding="utf-8",
    extra="ignore",
    populate_by_name=True,
)


_DESKTOP_CONFIG_WRITE_ENABLED = "REPOPILOT_DESKTOP_CONFIG_WRITE_ENABLED"
_MANAGED_CONFIG_FILE_NAME = "settings.env"
_RUNTIME_CONFIG_KEYS = (
    "REPOPILOT_CHAT_BASE_URL",
    "REPOPILOT_CHAT_API_KEY",
    "REPOPILOT_CHAT_MODEL",
    "REPOPILOT_EMBEDDING_BASE_URL",
    "REPOPILOT_EMBEDDING_API_KEY",
    "REPOPILOT_EMBEDDING_MODEL",
    "REPOPILOT_EMBEDDING_DIMENSIONS",
    "REPOPILOT_QDRANT_URL",
)
_RUNTIME_CONFIG_LINE = re.compile(r"^(?P<prefix>\s*)(?P<key>REPOPILOT_[A-Z0-9_]+)\s*=.*$")


class RuntimeConfigurationError(ValueError):
    """桌面运行配置读写失败时的稳定错误码。"""


class RuntimeConfigurationManager:
    """只管理 Tauri 启动的 `settings.env`，不允许网页端改写开发仓库 `.env`。"""

    def __init__(self, environment: dict[str, str] | None = None) -> None:
        self._environment = environment if environment is not None else os.environ

    def snapshot(self) -> dict[str, object]:
        """返回可展示的配置状态，绝不返回 API Key 的原始值。"""

        try:
            settings = AppSettings()
            return {
                "status": "READY",
                "writable": self.is_writable(),
                "restart_required": False,
                "chat": {
                    "base_url": settings.chat_base_url or "",
                    "model": settings.chat_model or "",
                    "api_key_configured": self._secret_configured(settings.chat_api_key),
                },
                "embedding": {
                    "base_url": settings.embedding_base_url or "",
                    "model": settings.embedding_model or "",
                    "dimensions": settings.embedding_dimensions,
                    "api_key_configured": self._secret_configured(settings.embedding_api_key),
                },
                "qdrant": {"url": settings.qdrant_url},
            }
        except Exception:
            # 配置值的解析细节可能含有用户输入，不能作为 API 错误返回。
            return {
                "status": "BLOCKED",
                "code": "INVALID_CONFIGURATION",
                "message": "运行配置格式不正确，请检查字段格式。",
                "writable": self.is_writable(),
                "restart_required": False,
            }

    def update(self, values: dict[str, object]) -> dict[str, object]:
        """原子写入白名单字段，并返回不含密钥的状态快照。"""

        config_path = self._writable_config_path()
        sanitized = self._validate_values(values)
        if not sanitized:
            raise RuntimeConfigurationError("CONFIGURATION_UPDATE_EMPTY")
        existing_lines = config_path.read_text(encoding="utf-8").splitlines() if config_path.exists() else []
        pending = dict(sanitized)
        rendered: list[str] = []
        for line in existing_lines:
            match = _RUNTIME_CONFIG_LINE.match(line)
            key = match.group("key") if match else None
            if key in pending:
                rendered.append(f"{match.group('prefix')}{key}={json.dumps(pending.pop(key), ensure_ascii=True)}")
            else:
                rendered.append(line)
        if pending:
            if rendered and rendered[-1] != "":
                rendered.append("")
            rendered.append("# RepoPilot Desktop runtime configuration")
            rendered.extend(f"{key}={json.dumps(value, ensure_ascii=True)}" for key, value in pending.items())
        temporary_path = config_path.with_suffix(".env.tmp")
        try:
            temporary_path.write_text("\n".join(rendered) + "\n", encoding="utf-8")
            temporary_path.replace(config_path)
        except OSError as error:
            temporary_path.unlink(missing_ok=True)
            raise RuntimeConfigurationError("CONFIGURATION_WRITE_FAILED") from error
        result = self.snapshot()
        result["restart_required"] = True
        result["code"] = "CONFIGURATION_SAVED"
        result["message"] = "配置已保存；重启 RepoPilot Desktop 后才会应用新的模型和检索配置。"
        return result

    def is_writable(self) -> bool:
        try:
            self._writable_config_path()
            return True
        except RuntimeConfigurationError:
            return False

    def _writable_config_path(self) -> Path:
        configured = self._environment.get("REPOPILOT_CONFIG_FILE")
        if self._environment.get(_DESKTOP_CONFIG_WRITE_ENABLED) != "1" or not configured:
            raise RuntimeConfigurationError("CONFIGURATION_WRITE_NOT_MANAGED")
        path = Path(configured).expanduser()
        if not path.is_absolute() or path.name != _MANAGED_CONFIG_FILE_NAME:
            raise RuntimeConfigurationError("CONFIGURATION_WRITE_NOT_MANAGED")
        parent = path.parent.resolve()
        if not parent.is_dir():
            raise RuntimeConfigurationError("CONFIGURATION_DIRECTORY_UNAVAILABLE")
        resolved = path.resolve(strict=False)
        if resolved.parent != parent or resolved.name != _MANAGED_CONFIG_FILE_NAME:
            raise RuntimeConfigurationError("CONFIGURATION_WRITE_NOT_MANAGED")
        return resolved

    @staticmethod
    def _secret_configured(value: SecretStr | None) -> bool:
        return value is not None and bool(value.get_secret_value().strip())

    @staticmethod
    def _validate_values(values: dict[str, object]) -> dict[str, str]:
        validated: dict[str, str] = {}
        for key, value in values.items():
            if key not in _RUNTIME_CONFIG_KEYS or value is None:
                continue
            if key == "REPOPILOT_EMBEDDING_DIMENSIONS":
                if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65_536:
                    raise RuntimeConfigurationError("INVALID_EMBEDDING_DIMENSIONS")
                validated[key] = str(value)
                continue
            if not isinstance(value, str) or len(value) > 1_024 or any(character in value for character in ("\r", "\n", "\x00")):
                raise RuntimeConfigurationError("INVALID_CONFIGURATION_VALUE")
            # URL 与模型名不能提交空白值；API Key 允许空字符串用于显式清除。
            if key not in {"REPOPILOT_CHAT_API_KEY", "REPOPILOT_EMBEDDING_API_KEY"} and not value.strip():
                raise RuntimeConfigurationError("INVALID_CONFIGURATION_VALUE")
            validated[key] = value.strip() if key not in {"REPOPILOT_CHAT_API_KEY", "REPOPILOT_EMBEDDING_API_KEY"} else value
        return validated


def _configured_env_file() -> Path | str:
    """桌面 sidecar 可显式指定配置文件；CLI 继续兼容当前目录 `.env`。"""

    configured = os.environ.get("REPOPILOT_CONFIG_FILE")
    return Path(configured).expanduser() if configured else ".env"


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

    def __init__(self, **values: object) -> None:
        # 调用方显式传入 `_env_file=None` 时用于测试或纯环境变量运行，不能被默认值覆盖。
        values.setdefault("_env_file", _configured_env_file())
        super().__init__(**values)

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

    @field_validator("embedding_dimensions", mode="before")
    @classmethod
    def blank_embedding_dimensions_are_missing(cls, value: object) -> object:
        """桌面模板用空值表示未配置，不能因此让 sidecar 在启动时崩溃。"""

        return None if isinstance(value, str) and not value.strip() else value

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

    def __init__(self, **values: object) -> None:
        values.setdefault("_env_file", _configured_env_file())
        super().__init__(**values)


def sanitized_settings_error() -> ComponentCheck:
    """将 Pydantic 配置错误转换为不含原始输入的审计结果。"""

    return ComponentCheck(
        component="settings",
        ready=False,
        code="INVALID_CONFIGURATION",
        message="环境变量格式不正确，请检查配置名称和数值格式。",
    )
