"""AgentHandle 契约与 LoopAgentHandle 驱动器测试（阶段四 Step 2 第二片）。

覆盖：三种输入语义（followup/steer/inject）、inject 不单独唤醒、
领取顺序（next-turn 优先）、闩存唤醒重放、cancel 清空与回调、
驱动器错误收容、when_idle 阻塞语义。
"""

import unittest
from threading import Barrier, Event

from repopilot_guard.graph_impl.agent_handle import (
    AgentMessage,
    AgentStatus,
    Inbox,
    InboxTarget,
    LoopAgentHandle,
    MessageOrigin,
)


class _Recorder:
    def __init__(self, *, on_call=None) -> None:
        self.calls: list[tuple[tuple[AgentMessage, ...], InboxTarget]] = []
        self.done = Event()
        self._on_call = on_call

    def __call__(self, messages: tuple[AgentMessage, ...], target: InboxTarget) -> None:
        self.calls.append((messages, target))
        if self._on_call is not None:
            self._on_call(self)
        self.done.set()


def _msg(content: str, origin: MessageOrigin = MessageOrigin.USER) -> AgentMessage:
    return AgentMessage(content=content, origin=origin)


class InboxSemanticsTests(unittest.TestCase):
    def test_claim_only_clears_targeted_queue(self) -> None:
        inbox = Inbox()
        inbox.send(_msg("追问"), InboxTarget.NEXT_TURN)
        inbox.send(_msg("注入", MessageOrigin.SYSTEM), InboxTarget.NEXT_STEP)

        claimed = inbox.claim(InboxTarget.NEXT_TURN)

        self.assertEqual([m.content for m in claimed], ["追问"])
        self.assertEqual([m.content for m in inbox.next_step], ["注入"])
        self.assertTrue(inbox.has_pending)
        self.assertEqual(inbox.claim(InboxTarget.NEXT_STEP)[0].content, "注入")
        self.assertFalse(inbox.has_pending)

    def test_clear_empties_both_queues(self) -> None:
        inbox = Inbox()
        inbox.send(_msg("a"), InboxTarget.NEXT_TURN)
        inbox.send(_msg("b"), InboxTarget.NEXT_STEP)
        inbox.clear()
        self.assertFalse(inbox.has_pending)


class InputSemanticsTests(unittest.TestCase):
    def test_followup_wakes_driver_with_next_turn_claim(self) -> None:
        driver = _Recorder()
        handle = LoopAgentHandle(driver)

        handle.followup(_msg("帮我修这个 bug"))
        handle.when_idle()

        self.assertEqual(len(driver.calls), 1)
        messages, target = driver.calls[0]
        self.assertEqual([m.content for m in messages], ["帮我修这个 bug"])
        self.assertIs(target, InboxTarget.NEXT_TURN)
        self.assertIs(handle.status, AgentStatus.IDLE)

    def test_inject_alone_does_not_wake_driver(self) -> None:
        driver = _Recorder()
        handle = LoopAgentHandle(driver)

        handle.inject(_msg("当前时间 14:32", MessageOrigin.SYSTEM))
        handle.when_idle()  # 立即返回：从未进入 running

        self.assertEqual(driver.calls, [], "inject 不得单独触发模型调用")
        self.assertEqual([m.content for m in handle.inbox.next_step], ["当前时间 14:32"])

    def test_inject_is_delivered_with_next_wake(self) -> None:
        driver = _Recorder()
        handle = LoopAgentHandle(driver)

        handle.inject(_msg("档案快照", MessageOrigin.SYSTEM))
        handle.followup(_msg("继续"))
        handle.when_idle()

        # next-turn 优先领取，注入上下文留在下一步骤边界被领取（对齐 dsh inbox 语义）。
        self.assertEqual(len(driver.calls), 2)
        first_messages, first_target = driver.calls[0]
        second_messages, second_target = driver.calls[1]
        self.assertEqual([m.content for m in first_messages], ["继续"])
        self.assertIs(first_target, InboxTarget.NEXT_TURN)
        self.assertEqual([m.content for m in second_messages], ["档案快照"])
        self.assertIs(second_target, InboxTarget.NEXT_STEP)

    def test_steer_wakes_with_next_step_claim(self) -> None:
        driver = _Recorder()
        handle = LoopAgentHandle(driver)

        handle.steer(_msg("别改那个文件"))
        handle.when_idle()

        self.assertEqual(len(driver.calls), 1)
        messages, target = driver.calls[0]
        self.assertEqual([m.content for m in messages], ["别改那个文件"])
        self.assertIs(target, InboxTarget.NEXT_STEP)


class LifecycleTests(unittest.TestCase):
    def test_latched_wake_replays_after_convergence(self) -> None:
        """运行期间到达的唤醒被闩存，当前轮收敛后重放（对齐 dsh wakeDriver）。"""
        barrier = Barrier(2)

        def wait_at_first_call(recorder: _Recorder) -> None:
            if len(recorder.calls) == 1:
                barrier.wait(timeout=5)

        driver = _Recorder(on_call=wait_at_first_call)
        handle = LoopAgentHandle(driver)

        handle.followup(_msg("第一问"))
        barrier.wait(timeout=5)  # 等驱动器停在第一次调用中
        handle.followup(_msg("第二问"))  # 运行中唤醒 → 闩存
        handle.when_idle()

        self.assertEqual([m.content for messages, _ in driver.calls for m in messages], ["第一问", "第二问"])

    def test_cancel_clears_inbox_and_notifies(self) -> None:
        reasons: list[str] = []
        driver = _Recorder()
        handle = LoopAgentHandle(driver, on_cancel=reasons.append)

        handle.inject(_msg("注入", MessageOrigin.SYSTEM))
        handle.cancel("用户请求取消任务。")
        handle.when_idle()

        self.assertFalse(handle.inbox.has_pending)
        self.assertEqual(reasons, ["用户请求取消任务。"])
        self.assertEqual(driver.calls, [])

    def test_driver_failure_is_contained_and_handle_stays_usable(self) -> None:
        errors: list[BaseException] = []

        def failing_driver(messages: tuple[AgentMessage, ...], target: InboxTarget) -> None:
            raise RuntimeError("模型通道故障")

        handle = LoopAgentHandle(failing_driver, on_error=errors.append)
        handle.followup(_msg("触发故障"))
        handle.when_idle()

        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)
        self.assertIs(handle.status, AgentStatus.IDLE)

        # 故障后可继续使用：换成正常驱动器语义验证状态机复位。
        driver = _Recorder()
        recovered = LoopAgentHandle(driver)
        recovered.followup(_msg("恢复"))
        recovered.when_idle()
        self.assertEqual(len(driver.calls), 1)

    def test_status_reports_running_during_execution(self) -> None:
        release = Event()

        def blocking_driver(messages: tuple[AgentMessage, ...], target: InboxTarget) -> None:
            release.wait(timeout=5)

        handle = LoopAgentHandle(blocking_driver)
        handle.followup(_msg("慢任务"))

        deadline_ok = _wait_until(lambda: handle.status is AgentStatus.RUNNING)
        self.assertTrue(deadline_ok, "驱动器运行期间状态应为 running")

        release.set()
        handle.when_idle()
        self.assertIs(handle.status, AgentStatus.IDLE)


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


if __name__ == "__main__":
    unittest.main()
