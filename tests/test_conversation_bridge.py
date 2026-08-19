"""ConversationStoreBridge 生产适配器测试（接线片 ②）。

覆盖三方法语义：load_context 返回历史投影、record_request 写入会话、
record_result 写入任务摘要且幂等；并验证非终态状态不写回。

设计：用 TemporaryDirectory + 真实 ConversationStore 钉死语义——不
mock，验证桥接器与真实 store 的协作行为。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from repopilot_guard.conversation_store import ConversationStore
from repopilot_guard.graph_impl.conversation_bridge import ConversationStoreBridge


def _make_store_and_conversation(directory: Path, *, project_id: str | None = "p1"):
    """建一个真实 ConversationStore + 一个会话，返回 (store, conversation_id)。"""
    store = ConversationStore(directory / "state.sqlite")
    conversation = store.create(project_id=project_id, display_title="测试会话", mode="goal")
    return store, conversation.conversation_id


class LoadContextTests(unittest.TestCase):
    def test_load_context_returns_empty_for_new_conversation(self) -> None:
        """新会话没有历史 → load_context 返回空串。"""
        with tempfile.TemporaryDirectory() as d:
            store, cid = _make_store_and_conversation(Path(d))
            try:
                bridge = ConversationStoreBridge(store, conversation_id=cid)
                self.assertEqual(bridge.load_context(), "")
            finally:
                store.close()

    def test_load_context_returns_history_after_messages(self) -> None:
        """会话有历史消息 → load_context 返回非空投影字符串。"""
        with tempfile.TemporaryDirectory() as d:
            store, cid = _make_store_and_conversation(Path(d))
            try:
                store.append_chat_request(cid, content="先帮我看看项目结构")
                store.append_chat_response(cid, content="好的，这是订单模块的入口...")
                bridge = ConversationStoreBridge(store, conversation_id=cid)
                context = bridge.load_context()
                self.assertIn("不可信上下文", context)  # model_message 的固定标记
                self.assertIn("先帮我看看项目结构", context)
            finally:
                store.close()


class RecordRequestTests(unittest.TestCase):
    def test_record_request_appends_task_request_message(self) -> None:
        """record_request 写入 task_request 消息。"""
        with tempfile.TemporaryDirectory() as d:
            store, cid = _make_store_and_conversation(Path(d))
            try:
                bridge = ConversationStoreBridge(store, conversation_id=cid)
                bridge.record_request("修复订单 bug")

                messages = store.messages(cid)
                # 第一条可能是会话标题消息；找 task_request
                task_requests = [m for m in messages if m.kind == "task_request"]
                self.assertEqual(len(task_requests), 1)
                self.assertEqual(task_requests[0].content, "修复订单 bug")
                self.assertEqual(task_requests[0].role, "user")
            finally:
                store.close()

    def test_record_request_multiple_times_appends_multiple_messages(self) -> None:
        """多次 record_request 追加多条 task_request（不幂等——每次追问都记一笔）。"""
        with tempfile.TemporaryDirectory() as d:
            store, cid = _make_store_and_conversation(Path(d))
            try:
                bridge = ConversationStoreBridge(store, conversation_id=cid)
                bridge.record_request("第一个问题")
                bridge.record_request("第二个问题")

                messages = store.messages(cid)
                task_requests = [m for m in messages if m.kind == "task_request"]
                self.assertEqual(len(task_requests), 2)
            finally:
                store.close()


class RecordResultTests(unittest.TestCase):
    def test_record_result_writes_summary_for_terminal_status(self) -> None:
        """终态状态（PASSED）→ 写入 task_summary 消息。"""
        with tempfile.TemporaryDirectory() as d:
            store, cid = _make_store_and_conversation(Path(d))
            try:
                bridge = ConversationStoreBridge(store, conversation_id=cid)
                result = SimpleNamespace(verdict="修复完成")
                bridge.record_result("thread-1", "PASSED", result)

                messages = store.messages(cid)
                summaries = [m for m in messages if m.kind == "task_summary"]
                self.assertEqual(len(summaries), 1)
                self.assertEqual(summaries[0].task_thread_id, "thread-1")
                self.assertEqual(summaries[0].task_status, "PASSED")
                self.assertEqual(summaries[0].task_verdict, "修复完成")
                self.assertIn("PASSED", summaries[0].content)
                self.assertIn("修复完成", summaries[0].content)
            finally:
                store.close()

    def test_record_result_idempotent_for_same_thread_id(self) -> None:
        """同一 thread_id 多次 record_result → 只写一条 task_summary（幂等）。"""
        with tempfile.TemporaryDirectory() as d:
            store, cid = _make_store_and_conversation(Path(d))
            try:
                bridge = ConversationStoreBridge(store, conversation_id=cid)
                result = SimpleNamespace(verdict="OK")
                bridge.record_result("thread-1", "PASSED", result)
                bridge.record_result("thread-1", "PASSED", result)  # 重复
                bridge.record_result("thread-1", "FAILED", result)  # 再重复

                messages = store.messages(cid)
                summaries = [m for m in messages if m.kind == "task_summary"]
                self.assertEqual(len(summaries), 1)  # 幂等
            finally:
                store.close()

    def test_record_result_skips_non_terminal_status(self) -> None:
        """非终态状态（RUNNING / WAITING_APPROVAL）→ 不写回。"""
        with tempfile.TemporaryDirectory() as d:
            store, cid = _make_store_and_conversation(Path(d))
            try:
                bridge = ConversationStoreBridge(store, conversation_id=cid)
                bridge.record_result("thread-1", "RUNNING", SimpleNamespace(verdict=None))
                bridge.record_result("thread-1", "WAITING_APPROVAL", SimpleNamespace(verdict=None))

                messages = store.messages(cid)
                summaries = [m for m in messages if m.kind == "task_summary"]
                self.assertEqual(len(summaries), 0)  # 非终态不写
            finally:
                store.close()

    def test_record_result_handles_dict_result(self) -> None:
        """result 是 dict（GraphRunResult.to_dict()）时也能取 verdict。"""
        with tempfile.TemporaryDirectory() as d:
            store, cid = _make_store_and_conversation(Path(d))
            try:
                bridge = ConversationStoreBridge(store, conversation_id=cid)
                bridge.record_result("thread-1", "BLOCKED", {"verdict": "依赖缺失"})

                messages = store.messages(cid)
                summaries = [m for m in messages if m.kind == "task_summary"]
                self.assertEqual(len(summaries), 1)
                self.assertEqual(summaries[0].task_verdict, "依赖缺失")
            finally:
                store.close()

    def test_record_result_handles_none_result(self) -> None:
        """result 为 None（失败路径）→ verdict=None，仍写回 task_summary。"""
        with tempfile.TemporaryDirectory() as d:
            store, cid = _make_store_and_conversation(Path(d))
            try:
                bridge = ConversationStoreBridge(store, conversation_id=cid)
                bridge.record_result("thread-1", "FAILED", None)

                messages = store.messages(cid)
                summaries = [m for m in messages if m.kind == "task_summary"]
                self.assertEqual(len(summaries), 1)
                self.assertIsNone(summaries[0].task_verdict)
            finally:
                store.close()


class ConversationIdBindingTests(unittest.TestCase):
    def test_bridge_binds_to_specific_conversation(self) -> None:
        """桥接器绑定单个 conversation_id，不影响其它会话。"""
        with tempfile.TemporaryDirectory() as d:
            store = ConversationStore(Path(d) / "state.sqlite")
            try:
                conv_a = store.create(project_id="p1", display_title="A", mode="goal")
                conv_b = store.create(project_id="p1", display_title="B", mode="goal")
                bridge_a = ConversationStoreBridge(store, conversation_id=conv_a.conversation_id)
                bridge_b = ConversationStoreBridge(store, conversation_id=conv_b.conversation_id)

                bridge_a.record_request("问题给 A")
                bridge_b.record_request("问题给 B")

                msgs_a = [m for m in store.messages(conv_a.conversation_id) if m.kind == "task_request"]
                msgs_b = [m for m in store.messages(conv_b.conversation_id) if m.kind == "task_request"]
                self.assertEqual(len(msgs_a), 1)
                self.assertEqual(len(msgs_b), 1)
                self.assertEqual(msgs_a[0].content, "问题给 A")
                self.assertEqual(msgs_b[0].content, "问题给 B")
                # 桥接器绑定的 conversation_id 属性
                self.assertEqual(bridge_a.conversation_id, conv_a.conversation_id)
                self.assertEqual(bridge_b.conversation_id, conv_b.conversation_id)
            finally:
                store.close()


class EndToEndWithLoadAfterRecordTests(unittest.TestCase):
    def test_record_then_load_context_reflects_recorded_messages(self) -> None:
        """record_request → record_result → load_context 反映写入的消息。"""
        with tempfile.TemporaryDirectory() as d:
            store, cid = _make_store_and_conversation(Path(d))
            try:
                bridge = ConversationStoreBridge(store, conversation_id=cid)

                # 第一轮：记录请求 + 结果
                bridge.record_request("修复订单地址校验 bug")
                bridge.record_result("thread-1", "PASSED", SimpleNamespace(verdict="已修复"))

                # 第二轮：load_context 应该能看到第一轮的请求和摘要
                context = bridge.load_context()
                self.assertIn("修复订单地址校验 bug", context)
                self.assertIn("PASSED", context)
                self.assertIn("已修复", context)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
