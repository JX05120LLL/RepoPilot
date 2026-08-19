"""GraphRunner ↔ AgentHandle 桥接器（阶段四 Step 2 第三片，DR-024）。

把现有的「组装会话上下文 → 跑任务 → 受控结论写回会话」循环收进
`AgentHandle` 契约：

- ``followup``（用户追问）→ 领取消息 → 组装上下文（会话历史 + 已注入
  上下文）→ 构造任务 → ``runner.run`` → 写回会话 → 回到空闲；
- ``inject`` 的上下文不单独触发任务，在本轮组装上下文时一并带走；
- ``steer`` 的插话消息按用户消息处理（当前引擎不支持步骤中途纠偏，
  语义上等价于一次快速追问，后续引擎升级后再细化）；
- 任务停在审批时 handle 进入 ``AWAITING_APPROVAL``，``approve()`` 走
  ``runner.resume`` 继续（支持两级审批的多次暂停）；
- ``cancel`` 清空收件箱并经 ``TaskCancellationRegistry`` 发进程内取消
  信号——持久取消状态属于 TaskStore，由接线方（api.py）负责。

本模块只依赖结构化协议（BridgeRunner / ConversationBridge），测试以
fake 实现覆盖；生产接线为后续片。
"""

from __future__ import annotations

from threading import Condition, Thread
from typing import Any, Callable, Protocol

from repopilot_guard.cancellation import DEFAULT_CANCELLATION_REGISTRY, TaskCancellationRegistry

from .agent_handle import (
    AgentMessage,
    AgentStatus,
    Inbox,
    InboxTarget,
    LoopAgentHandle,
    MessageOrigin,
)


class BridgeRunner(Protocol):
    """GraphRunner 的结构化视图；接线方保证 run/resume 语义与 GraphRunner 一致。"""

    def run(self, request: Any, thread_id: str, permission: Any = None) -> Any: ...

    def resume(self, thread_id: str, approved: bool) -> Any: ...


class ConversationBridge(Protocol):
    """会话侧回调；生产实现包 ConversationStore，测试注入 fake。"""

    def load_context(self) -> str:
        """读取下一轮任务可用的会话上下文（对齐 context_for_next_task().model_message()）。"""
        ...

    def record_request(self, content: str) -> None:
        """记录用户本轮输入（对齐 append_task_request）。"""
        ...

    def record_result(self, thread_id: str, status: str, result: Any) -> None:
        """写回受控任务结论（对齐 append_task_summary，只取受控状态）。"""
        ...


RequestFactory = Callable[[str, str], tuple[Any, str]]
"""(description, context) -> (request, thread_id)。"""


class GraphAgentHandle:
    """以 AgentHandle 契约驱动的 GraphRunner 会话桥。

    线程模型：追问轮跑在 LoopAgentHandle 的驱动线程上；审批恢复
    （approve）单独起线程执行 resume，两者不会同时进行——等待审批时
    驱动循环已收敛，approve 才会启动恢复线程。
    """

    def __init__(
        self,
        runner: BridgeRunner,
        request_factory: RequestFactory,
        *,
        permission: Any = None,
        conversation: ConversationBridge | None = None,
        cancellation_registry: TaskCancellationRegistry | None = None,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        self._runner = runner
        self._request_factory = request_factory
        self._permission = permission
        self._conversation = conversation
        self._registry = cancellation_registry or DEFAULT_CANCELLATION_REGISTRY
        self._on_error = on_error
        self._cond = Condition()
        self._awaiting_approval = False
        self._resuming = False
        self._active_thread_id: str | None = None
        self._loop = LoopAgentHandle(self._run_round, on_cancel=self._cancel_active, on_error=on_error)

    # ------------------------------------------------------------------
    # AgentHandle 契约
    # ------------------------------------------------------------------

    @property
    def inbox(self) -> Inbox:
        return self._loop.inbox

    def followup(self, message: AgentMessage) -> None:
        self._loop.followup(message)

    def steer(self, message: AgentMessage) -> None:
        self._loop.steer(message)

    def inject(self, message: AgentMessage) -> None:
        self._loop.inject(message)

    def cancel(self, reason: str = "") -> None:
        self._loop.cancel(reason)

    @property
    def status(self) -> AgentStatus:
        with self._cond:
            if self._awaiting_approval:
                return AgentStatus.AWAITING_APPROVAL
            if self._resuming:
                return AgentStatus.RUNNING
        return self._loop.status

    @property
    def active_thread_id(self) -> str | None:
        with self._cond:
            return self._active_thread_id

    def when_idle(self) -> None:
        """等待驱动循环与审批恢复线程全部收敛；等待审批视为已收敛。"""
        self._loop.when_idle()
        with self._cond:
            while self._resuming:
                self._cond.wait()

    # ------------------------------------------------------------------
    # 审批
    # ------------------------------------------------------------------

    def approve(self, approved: bool) -> None:
        """恢复等待审批的任务；resume 可能再次停在下一级审批。"""
        with self._cond:
            if not self._awaiting_approval:
                raise ValueError("NO_PENDING_APPROVAL")
            thread_id = self._active_thread_id
            self._awaiting_approval = False
            self._resuming = True
            self._cond.notify_all()
        assert thread_id is not None
        Thread(target=self._resume, args=(thread_id, approved), name="repopilot-agent-resume", daemon=True).start()

    def _resume(self, thread_id: str, approved: bool) -> None:
        try:
            result = self._runner.resume(thread_id, approved)
            self._conclude(result, thread_id)
        except BaseException as error:  # noqa: BLE001 —— 恢复线程边界收容失败
            if self._on_error is not None:
                self._on_error(error)
        finally:
            with self._cond:
                self._resuming = False
                self._cond.notify_all()

    # ------------------------------------------------------------------
    # 任务轮
    # ------------------------------------------------------------------

    def _run_round(self, messages: tuple[AgentMessage, ...], target: InboxTarget) -> None:
        # 先把已到达的注入上下文带走（留在队列里的用户插话由后续轮处理）。
        injected = self._loop.inbox.drain(InboxTarget.NEXT_STEP, lambda m: m.origin is MessageOrigin.SYSTEM)
        user_texts = [m.content for m in messages if m.origin is MessageOrigin.USER]
        if not user_texts:
            return  # 纯注入/空轮：不单独消耗一次任务
        description = "\n".join(user_texts)

        context_parts: list[str] = []
        if self._conversation is not None:
            history = self._conversation.load_context()
            if history:
                context_parts.append(history)
        context_parts.extend(m.content for m in injected)
        if self._conversation is not None:
            self._conversation.record_request(description)

        request, thread_id = self._request_factory(description, "\n\n".join(context_parts))
        with self._cond:
            self._active_thread_id = thread_id
            self._cond.notify_all()
        try:
            result = self._runner.run(request, thread_id, self._permission)
        except BaseException:
            with self._cond:
                self._active_thread_id = None
                self._cond.notify_all()
            raise
        self._conclude(result, thread_id)

    def _conclude(self, result: Any, thread_id: str) -> None:
        pending = bool(getattr(result, "pending_approval", False))
        with self._cond:
            self._awaiting_approval = pending
            if not pending:
                self._active_thread_id = None
            self._cond.notify_all()
        if not pending and self._conversation is not None:
            self._conversation.record_result(thread_id, str(getattr(result, "status", "")), result)

    def _cancel_active(self, reason: str) -> None:
        with self._cond:
            thread_id = self._active_thread_id
        if thread_id is not None:
            self._registry.request(thread_id, reason or None)


__all__ = [
    "BridgeRunner",
    "ConversationBridge",
    "GraphAgentHandle",
    "RequestFactory",
]
