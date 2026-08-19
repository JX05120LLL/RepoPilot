"""Agent 编排契约与输入收件箱（阶段四 Step 2 第二片，DR-023）。

对齐 DeepSeek Harness `core/agent` 的设计：

- **AgentHandle**：不依赖具体循环实现的 Agent 契约词汇——三种输入语义
  （followup / steer / inject）、取消、状态与空闲等待。LangGraph 图是未来的
  可替换实现之一，契约本身不含编排细节。
- **Inbox**：线程安全的输入收件箱。三种投递语义来自 dsh：
  - ``followup``：用户追问，进 next-turn 队列，立即唤醒驱动器；
  - ``steer``：用户插话纠偏，进 next-step 队列，立即唤醒驱动器；
  - ``inject``：系统注入上下文，进 next-step 队列，**不唤醒**——单独一坨
    上下文不值得消耗一次模型调用，等下一条真正唤醒的消息到来时一起领取。
- **LoopAgentHandle**：通用最小驱动器，把「领取输入 → 调用 driver →
  检查是否欠后续工作」做成骨架；driver 由装配方注入（未来是 GraphRunner
  的桥接），本模块不含任何 Coding 业务逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from threading import Condition, Thread
from typing import Callable, Protocol


class AgentStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"


class InboxTarget(StrEnum):
    NEXT_TURN = "next-turn"
    NEXT_STEP = "next-step"


class MessageOrigin(StrEnum):
    USER = "user"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class AgentMessage:
    """投递给 Agent 的一条输入；投递后不可变。"""

    content: str
    origin: MessageOrigin = MessageOrigin.USER


Driver = Callable[[tuple[AgentMessage, ...], InboxTarget], None]
"""驱动器回调：收到本轮领取的消息与领取目标；异常由 handle 收容并回到 idle。"""


class Inbox:
    """按目标分队列的线程安全收件箱。

    语义对齐 dsh Inbox：注入的上下文留在 next-step 队列中，直到另一条
    唤醒消息到来才被领取；取消时清空两个队列。
    """

    def __init__(self) -> None:
        self._lock = Condition()
        self._next_turn: list[AgentMessage] = []
        self._next_step: list[AgentMessage] = []

    def send(self, message: AgentMessage, target: InboxTarget) -> None:
        with self._lock:
            (self._next_turn if target is InboxTarget.NEXT_TURN else self._next_step).append(message)
            self._lock.notify_all()

    def claim(self, target: InboxTarget) -> tuple[AgentMessage, ...]:
        """取空指定目标队列；不跨队列合并，合并顺序由驱动器决定。"""
        with self._lock:
            queue = self._next_turn if target is InboxTarget.NEXT_TURN else self._next_step
            claimed = tuple(queue)
            queue.clear()
            return claimed

    def drain(self, target: InboxTarget, predicate: Callable[[AgentMessage], bool]) -> tuple[AgentMessage, ...]:
        """按谓词选择性取出消息；不满足的消息留在原队列。"""
        with self._lock:
            queue = self._next_turn if target is InboxTarget.NEXT_TURN else self._next_step
            picked = tuple(message for message in queue if predicate(message))
            remaining = [message for message in queue if not predicate(message)]
            queue.clear()
            queue.extend(remaining)
            return picked

    def clear(self) -> None:
        with self._lock:
            self._next_turn.clear()
            self._next_step.clear()
            self._lock.notify_all()

    @property
    def has_pending(self) -> bool:
        with self._lock:
            return bool(self._next_turn or self._next_step)

    @property
    def next_step(self) -> tuple[AgentMessage, ...]:
        with self._lock:
            return tuple(self._next_step)


class AgentHandle(Protocol):
    """Agent 的稳定契约；具体循环实现（LangGraph 或未来轻量实现）各自满足。"""

    inbox: Inbox

    def followup(self, message: AgentMessage) -> None:
        """用户追问：下一轮生效并立即唤醒。"""
        ...

    def steer(self, message: AgentMessage) -> None:
        """用户插话纠偏：下一步骤生效并立即唤醒。"""
        ...

    def inject(self, message: AgentMessage) -> None:
        """系统注入上下文：下一步骤生效，不单独唤醒模型调用。"""
        ...

    def cancel(self, reason: str = "") -> None:
        ...

    @property
    def status(self) -> AgentStatus: ...

    def when_idle(self) -> None:
        """阻塞直到驱动器空闲；重复唤醒期间持续等待。"""
        ...


class LoopAgentHandle:
    """通用最小驱动器：领取输入 → 调用注入的 driver → 检查欠账 → 回到空闲。

    - 同一时刻最多一个驱动线程；运行期间的新唤醒被闩住（latch），
      当前轮收敛后重放，语义对齐 dsh wakeDriver 的闩存机制。
    - driver 抛出的异常被收容在驱动器边界：状态回到 idle，错误经
      ``on_error`` 回调上报（fail-loud 的通道），绝不静默吞掉。
    """

    def __init__(
        self,
        driver: Driver,
        *,
        on_cancel: Callable[[str], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        self.inbox = Inbox()
        self._driver = driver
        self._on_cancel = on_cancel
        self._on_error = on_error
        self._cond = Condition()
        self._running = False
        self._wake_latched = False

    # ------------------------------------------------------------------
    # 三种输入语义
    # ------------------------------------------------------------------

    def followup(self, message: AgentMessage) -> None:
        self._send(message, InboxTarget.NEXT_TURN, wakeup=True)

    def steer(self, message: AgentMessage) -> None:
        self._send(message, InboxTarget.NEXT_STEP, wakeup=True)

    def inject(self, message: AgentMessage) -> None:
        self._send(message, InboxTarget.NEXT_STEP, wakeup=False)

    def _send(self, message: AgentMessage, target: InboxTarget, *, wakeup: bool) -> None:
        self.inbox.send(message, target)
        if wakeup:
            self._wake()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @property
    def status(self) -> AgentStatus:
        with self._cond:
            return AgentStatus.RUNNING if self._running else AgentStatus.IDLE

    def cancel(self, reason: str = "") -> None:
        """清空收件箱并通知外部取消钩子；正在收敛的轮次由 driver 自身协作退出。"""
        self.inbox.clear()
        with self._cond:
            self._wake_latched = False
        if self._on_cancel is not None:
            self._on_cancel(reason)

    def when_idle(self) -> None:
        with self._cond:
            while self._running or self._wake_latched:
                self._cond.wait()

    def _wake(self) -> None:
        with self._cond:
            if self._running:
                # 运行中的驱动器自己领取队列；唤醒闩存，收敛后重放。
                self._wake_latched = True
                self._cond.notify_all()
                return
            self._running = True
            self._cond.notify_all()
        Thread(target=self._drive, name="repopilot-agent-loop", daemon=True).start()

    def _drive(self) -> None:
        while self._converge():
            pass

    def _converge(self) -> bool:
        """运行一次唤醒的收敛过程；返回 True 表示需立即重放闩存的唤醒。"""
        try:
            while True:
                messages, target = self._claim_round()
                if not messages:
                    break
                try:
                    self._driver(messages, target)
                except BaseException as error:  # noqa: BLE001 —— 驱动器边界收容所有失败
                    if self._on_error is not None:
                        self._on_error(error)
                    break
                # driver 可能欠下后续工作（如运行期注入）；有欠账则继续领取。
                if not self.inbox.has_pending:
                    with self._cond:
                        if not self._wake_latched:
                            break
        finally:
            restart = False
            with self._cond:
                if self._wake_latched and self.inbox.has_pending:
                    self._wake_latched = False
                    restart = True
                else:
                    self._wake_latched = False
                    self._running = False
                self._cond.notify_all()
            return restart

    def _claim_round(self) -> tuple[tuple[AgentMessage, ...], InboxTarget]:
        """一轮领取：next-turn 优先（回合开始），其次 next-step（步骤边界）。"""
        claimed = self.inbox.claim(InboxTarget.NEXT_TURN)
        if claimed:
            return claimed, InboxTarget.NEXT_TURN
        claimed = self.inbox.claim(InboxTarget.NEXT_STEP)
        return claimed, InboxTarget.NEXT_STEP


__all__ = [
    "AgentHandle",
    "AgentMessage",
    "AgentStatus",
    "Driver",
    "Inbox",
    "InboxTarget",
    "LoopAgentHandle",
    "MessageOrigin",
]
