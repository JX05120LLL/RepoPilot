"""模型 Provider 抽象；第一阶段只构造客户端，不调用远程模型。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from repopilot_guard.config import AppSettings, ComponentCheck


@runtime_checkable
class ChatModelProvider(Protocol):
    """聊天模型 Provider 的最小内部接口。"""

    def check(self) -> ComponentCheck: ...

    def create_chat_model(self) -> ChatOpenAI: ...

    def chat_pricing(self) -> tuple[float, float, str] | None: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    """向量模型 Provider 的最小内部接口。"""

    def check(self) -> ComponentCheck: ...

    def create_embeddings(self) -> OpenAIEmbeddings: ...


class OpenAICompatibleProvider(ChatModelProvider, EmbeddingProvider):
    """适配 OpenAI 及兼容其 API 协议的模型服务。"""

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings

    def check(self) -> ComponentCheck:
        """兼容两个接口；调用方应优先使用对应的专用检查方法。"""

        chat = self.chat_check()
        return chat if not chat.ready else self.embedding_check()

    def chat_check(self) -> ComponentCheck:
        return self._settings.chat_check()

    def embedding_check(self) -> ComponentCheck:
        return self._settings.embedding_check()

    def create_chat_model(self) -> ChatOpenAI:
        check = self.chat_check()
        if not check.ready:
            raise ValueError(check.code)
        return ChatOpenAI(
            model=self._settings.chat_model,
            api_key=self._settings.chat_api_key.get_secret_value(),
            base_url=self._settings.chat_base_url,
            timeout=120.0,
            max_retries=1,
            max_tokens=4096,
        )

    def create_embeddings(self) -> OpenAIEmbeddings:
        check = self.embedding_check()
        if not check.ready:
            raise ValueError(check.code)
        return OpenAIEmbeddings(
            model=self._settings.embedding_model,
            api_key=self._settings.embedding_api_key.get_secret_value(),
            base_url=self._settings.embedding_base_url,
            dimensions=self._settings.embedding_dimensions,
            timeout=60.0,
            max_retries=1,
            # 百炼兼容接口要求 input 为文本；避免 LangChain 预先转为 token ID 数组。
            check_embedding_ctx_length=False,
        )

    def create_fallback_chat_model(self) -> ChatOpenAI | None:
        """主模型不可用时的备选模型；未配置备选时返回 None（不降级）。"""

        if not self.chat_fallback_check().ready:
            return None
        return ChatOpenAI(
            model=self._settings.chat_fallback_model,
            api_key=self._settings.chat_fallback_api_key.get_secret_value(),
            base_url=self._settings.chat_fallback_base_url,
            timeout=120.0,
            max_retries=1,
            max_tokens=4096,
        )

    def chat_fallback_check(self) -> ComponentCheck:
        missing = self._settings._missing(
            {
                "REPOPILOT_CHAT_FALLBACK_BASE_URL": self._settings.chat_fallback_base_url,
                "REPOPILOT_CHAT_FALLBACK_API_KEY": self._settings.chat_fallback_api_key,
                "REPOPILOT_CHAT_FALLBACK_MODEL": self._settings.chat_fallback_model,
            }
        )
        return self._settings._component_check("chat_fallback_provider", missing)

    def chat_pricing(self) -> tuple[float, float, str] | None:
        """价格只用于本机估算；两个方向未同时配置时不输出伪造费用。"""

        if self._settings.chat_input_price_per_million is None or self._settings.chat_output_price_per_million is None:
            return None
        return (
            self._settings.chat_input_price_per_million,
            self._settings.chat_output_price_per_million,
            self._settings.chat_price_currency,
        )
