"""阶段四 Step 2：graph.py 拆分出的子模块（由 graph.py 兼容 shim 统一重导出）。"""

from __future__ import annotations

import asyncio
import inspect
from contextlib import contextmanager
from contextvars import ContextVar
from queue import Empty, Queue
from threading import Event, Thread
from typing import Any, Callable

_ACTIVE_MODEL_CANCELLATION: ContextVar[Callable[[], bool] | None] = ContextVar(
    "repopilot_model_cancellation",
    default=None,
)

class ModelInvocationCancelled(RuntimeError):
    """模型请求已接到任务取消并停止等待结果。"""


@contextmanager
def _model_cancellation_scope(cancellation_requested: Callable[[], bool]):
    """按当前调用绑定取消信号，避免并发任务共享可变模型状态。"""

    token = _ACTIVE_MODEL_CANCELLATION.set(cancellation_requested)
    try:
        yield
    finally:
        _ACTIVE_MODEL_CANCELLATION.reset(token)


def _raise_if_model_cancelled() -> None:
    cancellation_requested = _ACTIVE_MODEL_CANCELLATION.get()
    if cancellation_requested is not None and cancellation_requested():
        raise ModelInvocationCancelled("MODEL_INVOCATION_CANCELLED")


def _invoke_model_request(model: Any, messages: list[dict[str, str]]) -> Any:
    """优先取消 async HTTP 调用；旧同步模型仍在返回后协作式停止。"""

    _raise_if_model_cancelled()
    cancellation_requested = _ACTIVE_MODEL_CANCELLATION.get()
    async_invoke = getattr(model, "ainvoke", None)
    if cancellation_requested is None or not callable(async_invoke):
        return model.invoke(messages)
    return _await_cancellable_model_response(lambda: async_invoke(messages), cancellation_requested)


def _await_cancellable_model_response(
    factory: Callable[[], object],
    cancellation_requested: Callable[[], bool],
) -> Any:
    """在独立事件循环内等待 LangChain 异步请求，取消时主动终止 coroutine。"""

    result_queue: Queue[tuple[bool, object]] = Queue(maxsize=1)
    ready = Event()
    cancel_requested = Event()
    control: dict[str, object] = {}

    async def resolve() -> Any:
        value = factory()
        if inspect.isawaitable(value):
            return await value
        return value

    def worker() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        task = loop.create_task(resolve())
        control["loop"] = loop
        control["task"] = task
        ready.set()
        if cancel_requested.is_set():
            loop.call_soon(task.cancel)
        try:
            result_queue.put((True, loop.run_until_complete(task)))
        except BaseException as error:
            result_queue.put((False, error))
        finally:
            if not task.done():
                task.cancel()
                loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
            loop.close()

    thread = Thread(target=worker, name="repopilot-model-request", daemon=True)
    thread.start()
    while True:
        try:
            succeeded, value = result_queue.get(timeout=0.05)
        except Empty:
            if not cancellation_requested():
                continue
            cancel_requested.set()
            if ready.wait(timeout=0.05):
                loop = control.get("loop")
                task = control.get("task")
                if isinstance(loop, asyncio.AbstractEventLoop) and isinstance(task, asyncio.Task):
                    loop.call_soon_threadsafe(task.cancel)
            # 不让取消 API 被异常 Provider 永久卡住；结果也绝不会再进入图状态。
            thread.join(timeout=0.25)
            raise ModelInvocationCancelled("MODEL_INVOCATION_CANCELLED")
        if succeeded:
            return value
        if cancellation_requested():
            raise ModelInvocationCancelled("MODEL_INVOCATION_CANCELLED")
        if isinstance(value, BaseException):
            raise value
        raise RuntimeError("模型异步调用返回了无效错误对象")
