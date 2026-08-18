"""GraphAgentHandle 桥接器测试（阶段四 Step 2 第三片）。

覆盖：追问轮组装会话上下文、inject 并入本轮上下文而不单独触发任务、
两级审批的多次暂停与 approve 恢复、取消的进程内信号、错误收容、
审批前置条件校验、Inbox.drain 选择性领取。
"""

import unittest
from types import SimpleNamespace

from repopilot_guard.cancellation import TaskCancellationRegistry
from repopilot_guard.graph_impl.agent_handle import AgentMessage, AgentStatus, Inbox, InboxTarget, MessageOrigin
from repopilot_guard.graph_impl.graph_bridge import GraphAgentHandle


class FakeResult:
    def __init__(self, *, pending_approval: bool = False, status: str = "COMPLETED") -> None:
        self.pending_approval = pending_approval
        self.status = status


class FakeRunner:
    def __init__(self, run_outcomes=None, resume_outcomes=None, run_error=None) -> None:
        self.run_outcomes = list(run_outcomes or [FakeResult()])
        self.resume_outcomes = list(resume_outcomes or [FakeResult()])
        self.run_error = run_error
        self.run_calls: list[tuple[object, str, object]] = []
        self.resume_calls: list[tuple[str, bool]] = []

    def run(self, request, thread_id, permission=None):
        self.run_calls.append((request, thread_id, permission))
        if self.run_error is not None:
            raise self.run_error
        return self.run_outcomes.pop(0)

    def resume(self, thread_id, approved):
        self.resume_calls.append((thread_id, approved))
        return self.resume_outcomes.pop(0)


class FakeConversation:
    def __init__(self, context: str = "上一轮总结") -> None:
        self._context = context
        self.requests: list[str] = []
        self.results: list[tuple[str, str]] = []

    def load_context(self) -> str:
        return self._context

    def record_request(self, content: str) -> None:
        self.requests.append(content)

    def record_result(self, thread_id: str, status: str, result) -> None:
        self.results.append((thread_id, status))


def _make_handle(runner, *, conversation=None, registry=None, on_error=None, permission="PERM"):
    calls: list[tuple[str, str]] = []

    def factory(description: str, context: str):
        calls.append((description, context))
        return SimpleNamespace(description=description, context=context), f"thread-{len(calls)}"

    handle = GraphAgentHandle(
        runner,
        factory,
        permission=permission,
        conversation=conversation,
        cancellation_registry=registry or TaskCancellationRegistry(),
        on_error=on_error,
    )
    return handle, calls


def _msg(content: str, origin: MessageOrigin = MessageOrigin.USER) -> AgentMessage:
    return AgentMessage(content=content, origin=origin)


class FollowupRoundTests(unittest.TestCase):
    def test_followup_runs_task_with_conversation_context(self) -> None:
        runner = FakeRunner()
        conversation = FakeConversation()
        handle, factory_calls = _make_handle(runner, conversation=conversation)

        handle.followup(_msg("修复订单校验"))
        handle.when_idle()

        self.assertEqual(len(runner.run_calls), 1)
        request, thread_id, permission = runner.run_calls[0]
        self.assertEqual(request.description, "修复订单校验")
        self.assertEqual(request.context, "上一轮总结")
        self.assertEqual(thread_id, "thread-1")
        self.assertEqual(permission, "PERM")
        self.assertEqual(factory_calls, [("修复订单校验", "上一轮总结")])
        self.assertEqual(conversation.requests, ["修复订单校验"])
        self.assertEqual(conversation.results, [("thread-1", "COMPLETED")])
        self.assertIs(handle.status, AgentStatus.IDLE)
        self.assertIsNone(handle.active_thread_id)

    def test_inject_does_not_trigger_task_and_joins_next_round_context(self) -> None:
        runner = FakeRunner()
        conversation = FakeConversation()
        handle, factory_calls = _make_handle(runner, conversation=conversation)

        handle.inject(_msg("能力档案快照", MessageOrigin.SYSTEM))
        handle.when_idle()
        self.assertEqual(runner.run_calls, [], "inject 不得单独触发任务")

        handle.followup(_msg("继续分析"))
        handle.when_idle()

        self.assertEqual(len(runner.run_calls), 1)
        description, context = factory_calls[0]
        self.assertEqual(description, "继续分析")
        self.assertIn("上一轮总结", context)
        self.assertIn("能力档案快照", context)
        self.assertFalse(handle.inbox.has_pending)


class ApprovalFlowTests(unittest.TestCase):
    def test_pending_approval_pauses_and_approve_resumes(self) -> None:
        runner = FakeRunner(run_outcomes=[FakeResult(pending_approval=True)], resume_outcomes=[FakeResult()])
        conversation = FakeConversation()
        handle, _ = _make_handle(runner, conversation=conversation)

        handle.followup(_msg("打个补丁"))
        handle.when_idle()

        self.assertIs(handle.status, AgentStatus.AWAITING_APPROVAL)
        self.assertEqual(handle.active_thread_id, "thread-1")
        self.assertEqual(conversation.results, [], "暂停期间不得写回终态结论")

        handle.approve(True)
        handle.when_idle()

        self.assertEqual(runner.resume_calls, [("thread-1", True)])
        self.assertEqual(conversation.results, [("thread-1", "COMPLETED")])
        self.assertIs(handle.status, AgentStatus.IDLE)
        self.assertIsNone(handle.active_thread_id)

    def test_two_stage_approval_pauses_twice(self) -> None:
        runner = FakeRunner(
            run_outcomes=[FakeResult(pending_approval=True)],
            resume_outcomes=[FakeResult(pending_approval=True), FakeResult()],
        )
        handle, _ = _make_handle(runner)

        handle.followup(_msg("修复并验证"))
        handle.when_idle()
        self.assertIs(handle.status, AgentStatus.AWAITING_APPROVAL)

        handle.approve(True)  # 计划审批
        handle.when_idle()
        self.assertIs(handle.status, AgentStatus.AWAITING_APPROVAL, "执行审批应再次暂停")

        handle.approve(True)  # 执行审批
        handle.when_idle()

        self.assertEqual(len(runner.resume_calls), 2)
        self.assertIs(handle.status, AgentStatus.IDLE)

    def test_approve_without_pending_approval_raises(self) -> None:
        handle, _ = _make_handle(FakeRunner())
        with self.assertRaisesRegex(ValueError, "NO_PENDING_APPROVAL"):
            handle.approve(True)


class CancellationAndFailureTests(unittest.TestCase):
    def test_cancel_signals_registry_for_active_task(self) -> None:
        registry = TaskCancellationRegistry()
        runner = FakeRunner(run_outcomes=[FakeResult(pending_approval=True)])
        handle, _ = _make_handle(runner, registry=registry)

        handle.followup(_msg("跑个任务"))
        handle.when_idle()
        thread_id = handle.active_thread_id
        self.assertIsNotNone(thread_id)
        registry.begin(thread_id)

        handle.cancel("用户请求取消任务。")
        handle.when_idle()

        self.assertTrue(registry.is_requested(thread_id))
        self.assertFalse(handle.inbox.has_pending)

    def test_runner_failure_is_contained_and_status_returns_idle(self) -> None:
        errors: list[BaseException] = []
        runner = FakeRunner(run_error=RuntimeError("模型通道故障"))
        handle, _ = _make_handle(runner, on_error=errors.append)

        handle.followup(_msg("触发故障"))
        handle.when_idle()

        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)
        self.assertIs(handle.status, AgentStatus.IDLE)
        self.assertIsNone(handle.active_thread_id)

    def test_resume_failure_is_contained(self) -> None:
        errors: list[BaseException] = []

        class FailingResumeRunner(FakeRunner):
            def resume(self, thread_id, approved):
                raise RuntimeError("恢复失败")

        runner = FailingResumeRunner(run_outcomes=[FakeResult(pending_approval=True)])
        handle, _ = _make_handle(runner, on_error=errors.append)

        handle.followup(_msg("需要审批"))
        handle.when_idle()
        handle.approve(True)
        handle.when_idle()

        self.assertEqual(len(errors), 1)
        self.assertIs(handle.status, AgentStatus.IDLE)


class InboxDrainTests(unittest.TestCase):
    def test_drain_picks_matching_messages_only(self) -> None:
        inbox = Inbox()
        inbox.send(_msg("注入", MessageOrigin.SYSTEM), InboxTarget.NEXT_STEP)
        inbox.send(_msg("插话"), InboxTarget.NEXT_STEP)

        picked = inbox.drain(InboxTarget.NEXT_STEP, lambda m: m.origin is MessageOrigin.SYSTEM)

        self.assertEqual([m.content for m in picked], ["注入"])
        self.assertEqual([m.content for m in inbox.next_step], ["插话"])


if __name__ == "__main__":
    unittest.main()
