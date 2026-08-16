"""阶段 1：可观测性埋点（OpenTelemetry 追踪 + Prometheus 指标）。

设计原则：

- 默认关闭：未设置 ``REPOPILOT_OTEL_ENABLED`` 时，所有入口都是零副作用 no-op；
- 依赖缺失（尚未 ``uv sync`` 新依赖）时自动降级为 no-op，绝不阻断业务；
- 只记录节点/模型调用的耗时、Token、成本与任务状态等可观测属性，
  绝不记录用户消息正文、文件内容或密钥。

追踪采用 OTLP/HTTP 导出（默认 ``http://127.0.0.1:4318/v1/traces``），指标采用
Prometheus 文本 exposition 格式，由 ``/metrics`` 端点暴露。
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

logger = logging.getLogger("repopilot.observability")

_TRUTHY = frozenset({"1", "true", "yes", "on"})

# 任务级 trace_id 用于日志关联；不承载任何密钥或正文。
_trace_id_var: ContextVar[str | None] = ContextVar("repopilot_trace_id", default=None)

# 延迟初始化；未开启或依赖缺失时保持为 None，走 no-op 路径。
_tracer: Any = None
_tracer_provider: Any = None
_metrics_registry: Any = None
_task_started_counter: Any = None
_task_terminal_counter: Any = None
_token_counter: Any = None
_cost_counter: Any = None
_initialized = False
_enabled = False


class _SpanHandle:
    """对 OpenTelemetry span 的受限封装；禁用时为 no-op，避免调用方到处判空。"""

    __slots__ = ("_span",)

    def __init__(self, span: Any) -> None:
        self._span = span

    def set_attribute(self, key: str, value: Any) -> None:
        if self._span is None or value is None:
            return
        try:
            self._span.set_attribute(key, value)
        except Exception:
            # span 属性类型不合法时不能影响任务执行。
            pass

    def set_attributes(self, attributes: dict[str, Any]) -> None:
        if self._span is None:
            return
        for key, value in attributes.items():
            self.set_attribute(key, value)

    def record_exception(self, error: BaseException) -> None:
        if self._span is None:
            return
        try:
            self._span.record_exception(error)
        except Exception:
            pass

    def set_status_error(self) -> None:
        if self._span is None:
            return
        try:
            from opentelemetry.trace import Status, StatusCode

            self._span.set_status(Status(StatusCode.ERROR))
        except Exception:
            pass


def init_observability() -> None:
    """初始化 TracerProvider 与 Prometheus registry（幂等，失败降级为 no-op）。"""

    global _initialized, _enabled
    global _tracer, _tracer_provider, _metrics_registry
    global _task_started_counter, _task_terminal_counter, _token_counter, _cost_counter
    if _initialized:
        return
    _initialized = True
    if os.environ.get("REPOPILOT_OTEL_ENABLED", "").strip().lower() not in _TRUTHY:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from prometheus_client import CollectorRegistry, Counter
    except Exception as error:
        logger.debug("可观测性依赖缺失，降级为 no-op：%s", error)
        return

    try:
        service_name = os.environ.get("REPOPILOT_OTEL_SERVICE_NAME", "repopilot-guard")
        endpoint = os.environ.get("REPOPILOT_OTEL_EXPORTER_ENDPOINT", "http://127.0.0.1:4318/v1/traces")
        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        trace.set_tracer_provider(provider)
        _tracer_provider = provider
        _tracer = trace.get_tracer("repopilot.guard")

        registry = CollectorRegistry()
        _task_started_counter = Counter("repopilot_tasks_started_total", "已提交任务总数", registry=registry)
        _task_terminal_counter = Counter(
            "repopilot_tasks_terminal_total", "进入终态的任务总数", ["status"], registry=registry
        )
        _token_counter = Counter(
            "repopilot_model_tokens_total", "模型 Token 消耗", ["type"], registry=registry
        )
        _cost_counter = Counter(
            "repopilot_model_cost_total", "模型预估成本", ["currency"], registry=registry
        )
        _metrics_registry = registry
        _enabled = True
    except Exception as error:
        logger.debug("可观测性初始化失败，降级为 no-op：%s", error)
        _enabled = False


def shutdown() -> None:
    """进程退出前冲刷尚未导出的 span；禁用时为 no-op。"""

    if _tracer_provider is None:
        return
    try:
        _tracer_provider.shutdown()
    except Exception:
        pass


def is_enabled() -> bool:
    return _enabled


@contextmanager
def span(name: str, attributes: dict[str, Any] | None = None) -> Iterator[_SpanHandle]:
    """启动一个命名 span；禁用或依赖缺失时退化为 no-op 上下文。"""

    tracer = _tracer
    if tracer is None:
        yield _SpanHandle(None)
        return
    with tracer.start_as_current_span(name) as active:
        handle = _SpanHandle(active)
        if attributes:
            handle.set_attributes(attributes)
        yield handle


def record_model_usage(
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    estimated_cost: float | None,
    currency: str | None,
) -> None:
    """记录一次模型调用的 Token 与预估成本指标。"""

    if not _enabled:
        return
    try:
        _token_counter.labels(type="input").inc(input_tokens)
        _token_counter.labels(type="output").inc(output_tokens)
        _token_counter.labels(type="total").inc(total_tokens)
        if estimated_cost is not None:
            _cost_counter.labels(currency=currency or "unknown").inc(estimated_cost)
    except Exception:
        pass


def record_task_started() -> None:
    if not _enabled:
        return
    try:
        _task_started_counter.inc()
    except Exception:
        pass


def record_task_terminal(status: str) -> None:
    if not _enabled:
        return
    try:
        _task_terminal_counter.labels(status=status).inc()
    except Exception:
        pass


def metrics_text() -> str:
    """渲染 Prometheus 文本格式；禁用时返回只读说明，避免端点报错。"""

    if not _enabled or _metrics_registry is None:
        return "# RepoPilot metrics disabled (REPOPILOT_OTEL_ENABLED not enabled)\n"
    try:
        from prometheus_client import generate_latest

        return generate_latest(_metrics_registry).decode("utf-8")
    except Exception:
        return "# RepoPilot metrics unavailable\n"


def set_trace_id(trace_id: str | None) -> None:
    _trace_id_var.set(trace_id)


def current_trace_id() -> str | None:
    return _trace_id_var.get()


def _reset_for_tests() -> None:
    """仅供测试：重置模块级单例状态，让每个用例可独立初始化。"""

    global _initialized, _enabled, _tracer, _tracer_provider, _metrics_registry
    global _task_started_counter, _task_terminal_counter, _token_counter, _cost_counter
    if _tracer_provider is not None:
        try:
            _tracer_provider.shutdown()
        except Exception:
            pass
    _initialized = False
    _enabled = False
    _tracer = None
    _tracer_provider = None
    _metrics_registry = None
    _task_started_counter = None
    _task_terminal_counter = None
    _token_counter = None
    _cost_counter = None
