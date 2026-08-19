"""本机任务的协作式取消信号。"""

from __future__ import annotations

from threading import Event, RLock


class TaskCancellationRegistry:
    """SQLite 是取消意图的持久事实，此注册表只负责唤醒当前进程内的执行器。"""

    def __init__(self) -> None:
        self._lock = RLock()
        self._events: dict[str, Event] = {}
        self._reasons: dict[str, str] = {}

    def begin(self, thread_id: str) -> None:
        """绑定运行时但不清除已有取消，避免取消与后台启动的竞态丢失。"""

        with self._lock:
            self._events.setdefault(thread_id, Event())

    def request(self, thread_id: str, reason: str | None = None) -> None:
        with self._lock:
            event = self._events.setdefault(thread_id, Event())
            self._reasons[thread_id] = _reason(reason)
            event.set()

    def is_requested(self, thread_id: str) -> bool:
        with self._lock:
            event = self._events.get(thread_id)
            return event.is_set() if event else False

    def reason(self, thread_id: str) -> str:
        with self._lock:
            return self._reasons.get(thread_id, "用户请求取消任务。")

    def release(self, thread_id: str) -> None:
        """任务退出后释放进程内信号；持久取消状态仍由 TaskStore 保存。"""

        with self._lock:
            self._events.pop(thread_id, None)
            self._reasons.pop(thread_id, None)


DEFAULT_CANCELLATION_REGISTRY = TaskCancellationRegistry()


def _reason(value: str | None) -> str:
    if not isinstance(value, str) or not value.strip():
        return "用户请求取消任务。"
    return value.strip()[:500]
