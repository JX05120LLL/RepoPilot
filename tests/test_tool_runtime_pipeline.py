"""ToolRuntime 双层管线测试（阶段四 Step 1）。

覆盖：pre-execute 钩子链（allow/deny/ask 三分决策）、审批降级语义、
单调守卫不可被钩子覆盖、post-execute 钩子（accept/block/替换 payload）、
注册即副作用（disposer）。
"""

import unittest

from langchain_core.tools import StructuredTool

from repopilot_guard.policy import PolicyDecision
from repopilot_guard.tool_runtime import (
    ApprovalOutcome,
    PostToolDecision,
    PreToolDecision,
    ToolExecution,
    ToolInvocationResult,
    ToolRuntime,
)


class _Spy:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def record(self, path: str) -> dict[str, object]:
        self.calls.append({"path": path})
        return {"status": "READY", "code": "TOOL_COMPLETED", "message": "执行成功。", "data": {}}


class _StaticApproval:
    def __init__(self, outcome: ApprovalOutcome) -> None:
        self._outcome = outcome
        self.requests: list[tuple[ToolExecution, str]] = []

    def request(self, execution: ToolExecution, reason: str) -> ApprovalOutcome:
        self.requests.append((execution, reason))
        return self._outcome


def _runtime(spy: _Spy, **kwargs) -> ToolRuntime:
    def spy_tool(path: str = "") -> dict[str, object]:
        return spy.record(path)

    tool = StructuredTool.from_function(spy_tool, name="spy_tool", description="测试用探针工具。")
    return ToolRuntime((tool,), **kwargs)


class PreExecuteHookTests(unittest.TestCase):
    def test_deny_short_circuits_and_blocks_execution(self) -> None:
        spy = _Spy()
        runtime = _runtime(spy, pre_hooks=[lambda execution: PreToolDecision.deny("ENV_FILE_BLOCKED", "禁止读取 .env 文件。")])

        result = runtime.invoke("spy_tool", {})

        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(result.code, "ENV_FILE_BLOCKED")
        self.assertIn(".env", result.payload["message"])
        self.assertEqual(spy.calls, [], "deny 后工具体不得执行")

    def test_allow_chain_executes_tool(self) -> None:
        spy = _Spy()
        seen: list[str] = []

        def first(execution: ToolExecution) -> PreToolDecision:
            seen.append("first")
            return PreToolDecision.allow()

        def second(execution: ToolExecution) -> PreToolDecision:
            seen.append("second")
            return PreToolDecision.allow()

        runtime = _runtime(spy, pre_hooks=[first, second])
        result = runtime.invoke("spy_tool", {"path": "src/Main.java"})

        self.assertEqual(result.status, "READY")
        self.assertEqual(seen, ["first", "second"])
        self.assertEqual(spy.calls, [{"path": "src/Main.java"}])

    def test_later_deny_overrides_earlier_allow(self) -> None:
        spy = _Spy()
        runtime = _runtime(
            spy,
            pre_hooks=[
                lambda execution: PreToolDecision.allow(),
                lambda execution: PreToolDecision.deny("LATE_BLOCKED", "后置钩子拒绝。"),
            ],
        )

        result = runtime.invoke("spy_tool", {})

        self.assertEqual(result.code, "LATE_BLOCKED")
        self.assertEqual(spy.calls, [])


class ApprovalDegradationTests(unittest.TestCase):
    def test_ask_without_approval_service_denies_as_unavailable(self) -> None:
        spy = _Spy()
        runtime = _runtime(spy, pre_hooks=[lambda execution: PreToolDecision.ask("写操作需要审批。")])

        result = runtime.invoke("spy_tool", {})

        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(result.code, "APPROVAL_UNAVAILABLE")
        self.assertEqual(spy.calls, [], "无审批通道必须拒绝，绝不默认放行")

    def test_ask_rejected_by_user_denies_with_distinct_code(self) -> None:
        spy = _Spy()
        approval = _StaticApproval(ApprovalOutcome.REJECTED)
        runtime = _runtime(spy, pre_hooks=[lambda execution: PreToolDecision.ask("写操作需要审批。")], approval_service=approval)

        result = runtime.invoke("spy_tool", {})

        self.assertEqual(result.code, "TOOL_APPROVAL_REJECTED")
        self.assertIn("用户拒绝", result.payload["message"])
        self.assertEqual(spy.calls, [])
        self.assertEqual(len(approval.requests), 1)
        self.assertEqual(approval.requests[0][1], "写操作需要审批。")

    def test_ask_cancelled_and_unavailable_have_distinct_codes(self) -> None:
        for outcome, expected in (
            (ApprovalOutcome.CANCELLED, "TOOL_APPROVAL_CANCELLED"),
            (ApprovalOutcome.UNAVAILABLE, "APPROVAL_UNAVAILABLE"),
        ):
            spy = _Spy()
            runtime = _runtime(spy, pre_hooks=[lambda execution: PreToolDecision.ask("需要审批。")], approval_service=_StaticApproval(outcome))
            result = runtime.invoke("spy_tool", {})
            self.assertEqual(result.code, expected, outcome)

    def test_ask_allowed_once_executes_tool(self) -> None:
        spy = _Spy()
        runtime = _runtime(
            spy,
            pre_hooks=[lambda execution: PreToolDecision.ask("写操作需要审批。")],
            approval_service=_StaticApproval(ApprovalOutcome.ALLOWED_ONCE),
        )

        result = runtime.invoke("spy_tool", {})

        self.assertEqual(result.status, "READY")
        self.assertEqual(len(spy.calls), 1)


class MonotonicGuardTests(unittest.TestCase):
    def test_guard_denial_cannot_be_overridden_by_allow_hook(self) -> None:
        """策略钩子 allow 不能放开守卫拒绝的调用——底线不容商量。"""
        spy = _Spy()
        runtime = _runtime(
            spy,
            pre_hooks=[lambda execution: PreToolDecision.allow()],
            guards=[lambda execution: PolicyDecision(False, "路径命中敏感后缀。", "PROTECTED_SUFFIX_BLOCKED")],
        )

        result = runtime.invoke("spy_tool", {})

        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(result.code, "PROTECTED_SUFFIX_BLOCKED")
        self.assertEqual(spy.calls, [])

    def test_guard_none_verdict_passes(self) -> None:
        spy = _Spy()
        runtime = _runtime(spy, guards=[lambda execution: None])

        result = runtime.invoke("spy_tool", {})

        self.assertEqual(result.status, "READY")
        self.assertEqual(len(spy.calls), 1)

    def test_guard_runs_after_ask_approval(self) -> None:
        """审批通过后守卫仍要复查——ask→allow 不等于越过底线。"""
        spy = _Spy()
        runtime = _runtime(
            spy,
            pre_hooks=[lambda execution: PreToolDecision.ask("需要审批。")],
            approval_service=_StaticApproval(ApprovalOutcome.ALLOWED_ONCE),
            guards=[lambda execution: PolicyDecision(False, "守卫拒绝。", "GUARD_BLOCKED")],
        )

        result = runtime.invoke("spy_tool", {})

        self.assertEqual(result.code, "GUARD_BLOCKED")
        self.assertEqual(spy.calls, [])


class PostExecuteHookTests(unittest.TestCase):
    def test_post_block_returns_blocked_feedback(self) -> None:
        spy = _Spy()
        runtime = _runtime(
            spy,
            post_hooks=[lambda execution, result: PostToolDecision.block("SENSITIVE_OUTPUT_BLOCKED", "输出包含敏感内容，已拦截。")],
        )

        result = runtime.invoke("spy_tool", {})

        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(result.code, "SENSITIVE_OUTPUT_BLOCKED")
        self.assertEqual(len(spy.calls), 1, "post 钩子在工具体执行后运行")

    def test_post_accept_replaces_payload(self) -> None:
        spy = _Spy()

        def redact(execution: ToolExecution, result: ToolInvocationResult) -> PostToolDecision:
            payload = dict(result.payload)
            payload["message"] = "输出已脱敏。"
            return PostToolDecision.accept(payload)

        runtime = _runtime(spy, post_hooks=[redact])
        result = runtime.invoke("spy_tool", {})

        self.assertEqual(result.status, "READY")
        self.assertEqual(result.payload["message"], "输出已脱敏。")

    def test_post_accept_without_payload_keeps_result(self) -> None:
        spy = _Spy()
        runtime = _runtime(spy, post_hooks=[lambda execution, result: PostToolDecision.accept()])

        result = runtime.invoke("spy_tool", {})

        self.assertEqual(result.status, "READY")
        self.assertEqual(result.payload["message"], "执行成功。")


class DisposerTests(unittest.TestCase):
    def test_disposer_unregisters_hooks_and_guards(self) -> None:
        spy = _Spy()
        runtime = _runtime(spy)
        dispose_deny = runtime.add_pre_execute_hook(lambda execution: PreToolDecision.deny("X", "拒绝。"))
        dispose_guard = runtime.add_guard(lambda execution: PolicyDecision(False, "守卫拒绝。", "G"))
        dispose_post = runtime.add_post_execute_hook(lambda execution, result: PostToolDecision.block("Y", "拒绝。"))

        self.assertEqual(runtime.invoke("spy_tool", {}).status, "BLOCKED")

        dispose_deny()
        self.assertEqual(runtime.invoke("spy_tool", {}).code, "G")

        dispose_guard()
        self.assertEqual(runtime.invoke("spy_tool", {}).code, "Y")

        dispose_post()
        self.assertEqual(runtime.invoke("spy_tool", {}).status, "READY")
        self.assertEqual(len(spy.calls), 2)  # post-block 那次已执行过工具体一次
        dispose_post()  # 重复撤销必须安全


if __name__ == "__main__":
    unittest.main()
