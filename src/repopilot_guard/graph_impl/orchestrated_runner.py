"""编排外壳：把 api.py 的临时工闭包转正为正式组件（阶段四 Step 2 接线片 ①）。

把 api.py ``run_in_background`` 与 ``resume_in_background`` 两个闭包里的 6 项编排
职责收进 ``OrchestratedGraphRunner``：

- 全局并发槽位（``BoundedSemaphore``，默认 2）——防大量任务同时启动挤爆本机；
- 租约与心跳——后台任务崩溃后能被自动回收，不悬死在 ``RUNNING``；
- 持久化（``store.sync_graph_result``）——终态写回 TaskStore；
- 取消完成（``store.complete_cancellation``）——区分取消与运行时失败；
- 运行时失败标记（``store.mark_runtime_failure``）——不把异常细节回桌面端；
- 可观测指标（``observability.record_*``）——排队 / 启动 / 终态打点。

实现 :class:`~repopilot_guard.graph_impl.graph_bridge.BridgeRunner` 协议
（``run`` / ``resume``），语义与 api.py 现有闭包零差异，便于片 ③ 接线
``GraphAgentHandle`` 时直接注入。

设计要点（对齐 api.py 930-963 / 1107-1135 现状）：

- ``run`` 走槽位（试拿→排队→阻塞等）；``resume`` 不走槽位——审批期间槽位
  仍被原 ``run`` 占着，恢复不需要重新排队；
- 心跳线程在 try 块开始、finally 块停；任何异常路径都停；
- 失败收容：区分 ``cancellation_requested_at``（取消完成）与其它（运行时
  失败标记），不把异常细节回桌面端；
- 会话写回：终态任务调 ``_append_conversation_task_summary``，会话投影失败
  不篡改任务结论。

本模块只依赖结构化协议（``GraphRunner`` / ``TaskStore`` / ``ConversationStore``
/ ``BoundedSemaphore`` / ``observability``），测试以 fake 实现钉死编排语义；
api.py 接线（feature flag 切换）属片 ③ 的事，本片保持现有路径不变。
"""

from __future__ import annotations

from threading import BoundedSemaphore, Event, Thread
from typing import TYPE_CHECKING, Any, Callable

from .runner import GraphRunResult, GraphRunner

if TYPE_CHECKING:  # 避免循环导入与重依赖
    from repopilot_guard.api import _append_conversation_task_summary
    from repopilot_guard.conversation_store import ConversationStore
    from repopilot_guard.observability import _Observability  # 仅用于类型提示
    from repopilot_guard.task_store import StoredTask, TaskStore


_DEFAULT_LEASE_HEARTBEAT_INTERVAL = 30.0
_DEFAULT_LEASE_JOIN_TIMEOUT = 1.0


class OrchestratedGraphRunner:
    """GraphRunner 的编排外壳，实现 BridgeRunner 协议。

    线程模型：``run`` / ``resume`` 都是**同步阻塞**方法——调用方（api.py
    的后台线程，或片 ③ 的 ``GraphAgentHandle`` 驱动循环）应在线程里调用。
    心跳起在独立 daemon 线程上，try 块开始、finally 块停。
    """

    def __init__(
        self,
        runner: GraphRunner,
        store: "TaskStore",
        conversations: "ConversationStore",
        slots: BoundedSemaphore,
        observability: Any,
        *,
        append_summary: "Callable[[ConversationStore, StoredTask, dict[str, object]], bool]" | None = None,
        lease_heartbeat_interval: float = _DEFAULT_LEASE_HEARTBEAT_INTERVAL,
        lease_join_timeout: float = _DEFAULT_LEASE_JOIN_TIMEOUT,
    ) -> None:
        self._runner = runner
        self._store = store
        self._conversations = conversations
        self._slots = slots
        self._observability = observability
        # 延迟绑定 api.py 的模块级辅助函数，便于测试注入 fake；不绑定时按名查找。
        self._append_summary = append_summary or _default_append_summary
        self._lease_heartbeat_interval = lease_heartbeat_interval
        self._lease_join_timeout = lease_join_timeout

    # ------------------------------------------------------------------
    # BridgeRunner 协议
    # ------------------------------------------------------------------

    def run(
        self,
        request: Any,
        thread_id: str,
        permission: Any = None,
    ) -> GraphRunResult:
        """对新任务跑图：排队拿槽位 → begin_execution → 心跳 → runner.run → 写回。

        语义对齐 api.py ``run_in_background`` 930-963 行：槽位满时
        ``record_task_queued`` 打点并阻塞等；``begin_execution`` 返回 False
        视为取消恰好先到，跳过 runner 直接返回（不抛异常以匹配现状）。
        """
        if not self._slots.acquire(blocking=False):
            self._observability.record_task_queued()
            self._slots.acquire()  # 阻塞等，直到有灶台释放
        try:
            if not _safe_begin_execution(self._store, thread_id):
                # 取消恰好先到——不调 runner，不写心跳；槽位仍要还。
                # 这里返回一个最小结果以匹配现状（api.py 闭包直接 return）。
                # 调用方（桥接器 / api.py）应从 store.get(thread_id) 取真实终态。
                return _aborted_result(thread_id)
            return self._run_with_lease(
                thread_id,
                lambda: self._runner.run(request, thread_id, permission),
            )
        finally:
            self._slots.release()

    def resume(
        self,
        thread_id: str,
        approved: bool,
        **resume_kwargs: Any,
    ) -> GraphRunResult:
        """恢复等待审批的任务：不重新排队，复用原 run 占用的槽位。

        语义对齐 api.py ``resume_in_background`` 1107-1135 行：审批期间
        槽位仍被原 ``run`` 占着（run 的 finally 还没到），所以 resume 不
        acquire/release——调用方保证此时槽位仍被持有（接线片 ③ 由
        ``GraphAgentHandle`` 的 approve 线程驱动，复用同一 handle 的生命周期）。

        当作为独立编排入口被 api.py 直接调用时（片 ① 不接线，但片 ③ 之前的
        过渡期可能用到），调用方负责保证槽位语义。
        """
        return self._run_with_lease(
            thread_id,
            lambda: self._runner.resume(thread_id, approved, **resume_kwargs),
        )

    # ------------------------------------------------------------------
    # 内部：心跳 + 失败收容 + 写回 + 指标（run/resume 共享）
    # ------------------------------------------------------------------

    def _run_with_lease(
        self,
        thread_id: str,
        body: "Callable[[], GraphRunResult]",
    ) -> GraphRunResult:
        """跑一次图（run 或 resume），配心跳与失败收容。

        - 心跳在 try 开始起、finally 停；
        - 成功：``sync_graph_result`` 持久化 → 取消完成或写会话摘要 → 指标；
        - 异常：区分取消与运行时失败 → 写回 + 指标 FAILED → 重新抛出（让
          调用方线程入口感知，但桌面端不会拿到异常细节）；
        - 会话投影失败不篡改任务结论（沿用 api.py 的 try/except）。
        """
        heartbeat_stop = Event()
        heartbeat = _start_lease_heartbeat(
            self._store,
            thread_id,
            heartbeat_stop,
            interval=self._lease_heartbeat_interval,
            join_timeout=self._lease_join_timeout,
        )
        try:
            result = body()
            stored = self._store.sync_graph_result(result.to_dict(), execution_finished=True)
            if stored.cancellation_requested_at:
                self._store.complete_cancellation(thread_id)
                stored = self._store.get(thread_id)
            self._append_summary(self._conversations, stored, result.to_dict())
            self._observability.record_task_terminal(stored.status)
            return result
        except Exception as error:
            # 不把异常细节或环境变量返回给桌面端；图自身的 BLOCKED 事件仍在 checkpoint。
            try:
                if self._store.get(thread_id).cancellation_requested_at:
                    self._store.complete_cancellation(thread_id)
                else:
                    self._store.mark_runtime_failure(
                        thread_id,
                        f"TASK_RUNTIME_FAILED: {type(error).__name__}",
                    )
                self._append_summary(self._conversations, self._store.get(thread_id), {})
                self._observability.record_task_terminal("FAILED")
            except ValueError:
                # 任务已被归档/删除：无法写回，静默退出（沿用 api.py 现状）。
                return _aborted_result(thread_id)
            raise
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=self._lease_join_timeout)


# ----------------------------------------------------------------------
# 模块级辅助：从 api.py 抽出的编排原语
# ----------------------------------------------------------------------


def _safe_begin_execution(store: "TaskStore", thread_id: str) -> bool:
    """对应 api.py ``_begin_execution``：begin_execution 失败视为取消先到。"""
    try:
        store.begin_execution(thread_id)
        return True
    except ValueError:
        return False


def _start_lease_heartbeat(
    store: "TaskStore",
    thread_id: str,
    stop: Event,
    *,
    interval: float = _DEFAULT_LEASE_HEARTBEAT_INTERVAL,
    join_timeout: float = _DEFAULT_LEASE_JOIN_TIMEOUT,
) -> Thread:
    """对应 api.py ``_start_lease_heartbeat``：30 秒一跳续租。"""

    def renew() -> None:
        # 最大 30 秒间隔，默认 15 分钟租约下可容忍 API 进程短暂阻塞。
        while not stop.wait(interval):
            try:
                store.renew_lease(thread_id)
            except ValueError:
                return  # 任务已被回收，退出心跳。

    worker = Thread(target=renew, name=f"repopilot-lease-{thread_id}", daemon=True)
    worker.start()
    return worker


def _default_append_summary(
    conversations: "ConversationStore",
    task: "StoredTask",
    graph_result: dict[str, object],
) -> bool:
    """默认绑定的会话写回：延迟 import api.py 的模块级函数。

    测试可注入 fake 替代。这里延迟 import 避免循环依赖（api.py 装配
    OrchestratedGraphRunner 时才需要这层绑定）。
    """
    from repopilot_guard.api import _append_conversation_task_summary

    return _append_conversation_task_summary(conversations, task, graph_result)


def _aborted_result(thread_id: str) -> GraphRunResult:
    """取消先到 / 任务已归档时的占位结果。

    调用方应从 ``store.get(thread_id)`` 取真实终态——这里只是为了让
    ``run``/``resume`` 在异常路径下也有返回值，匹配 api.py 现状（闭包里
    直接 ``return``，不抛异常给线程入口）。
    """
    return GraphRunResult(
        thread_id=thread_id,
        task_id="",
        status="ABORTED",
        pending_approval=False,
        verdict=None,
        state={},  # type: ignore[arg-type]
        interrupts=(),
    )


__all__ = ["OrchestratedGraphRunner"]
