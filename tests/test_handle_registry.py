"""HandleRegistry 测试（接线片 ③a）。

覆盖：线程安全、惰性创建、同会话复用、不同会话隔离、cancel_all_and_wait
优雅退出；fake runner 跑通完整会话循环（followup → run → writeback →
idle → 再 followup → run → writeback）。
"""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from repopilot_guard.conversation_store import ConversationStore
from repopilot_guard.cancellation import TaskCancellationRegistry
from repopilot_guard.graph_impl.agent_handle import AgentMessage, AgentStatus, MessageOrigin
from repopilot_guard.graph_impl.handle_registry import HandleRegistry, user_message


def _make_temp_dir() -> str:
    """Windows 上 TemporaryDirectory 在 sqlite 文件锁释放前清理会失败——用 mkdtemp + 手动忽略清理错误。"""
    return tempfile.mkdtemp(prefix="repopilot-test-")


def _safe_rmtree(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


class FakeResult:
    def __init__(self, *, pending_approval: bool = False, status: str = "PASSED", verdict: str = "OK") -> None:
        self.pending_approval = pending_approval
        self.status = status
        self.verdict = verdict

    def to_dict(self) -> dict[str, object]:
        return {"status": self.status, "verdict": self.verdict, "pending_approval": self.pending_approval}


class FakeOrchestrated:
    """对齐 OrchestratedGraphRunner 的 fake——记录 run/resume 调用，可注入结果。"""

    def __init__(self, *, run_outcomes=None, run_error=None) -> None:
        self.run_outcomes = list(run_outcomes or [FakeResult()])
        self.run_error = run_error
        self.run_calls: list[tuple[object, str, object]] = []

    def run(self, request, thread_id, permission=None):
        self.run_calls.append((request, thread_id, permission))
        if self.run_error is not None:
            raise self.run_error
        return self.run_outcomes.pop(0) if self.run_outcomes else FakeResult()

    def resume(self, thread_id, approved, **kwargs):
        return FakeResult()


def _make_factory(prefix: str = "thread"):
    """RequestFactory fake：记录调用，返回 (request, thread_id)。"""
    calls: list[tuple[str, str]] = []

    def factory(description: str, context: str):
        calls.append((description, context))
        thread_id = f"{prefix}-{len(calls)}"
        return SimpleNamespace(description=description, context=context), thread_id

    return factory, calls


def _make_registry(directory: Path, *, orchestrated=None):
    """建一个真实 ConversationStore + HandleRegistry。"""
    store = ConversationStore(directory / "state.sqlite")
    conv = store.create(project_id="p1", display_title="测试", mode="goal")
    orchestrated = orchestrated or FakeOrchestrated()
    factory, factory_calls = _make_factory()
    registry = HandleRegistry(
        orchestrated=orchestrated,  # type: ignore[arg-type]
        conversations=store,
        request_factory=factory,
        permission="PERM",
        cancellation_registry=TaskCancellationRegistry(),
    )
    return registry, store, conv.conversation_id, orchestrated, factory_calls


def _close_and_cleanup(registry: HandleRegistry, store: ConversationStore) -> None:
    """测试清理：cancel 所有 handle + 关 store + 给一点时间让线程退出。"""
    try:
        registry.cancel_all_and_wait(reason="test cleanup")
    except Exception:
        pass
    time.sleep(0.05)  # 让驱动线程真正退出
    try:
        store.close()
    except Exception:
        pass


class GetOrCreateTests(unittest.TestCase):
    def test_get_or_create_is_lazy_and_idempotent(self) -> None:
        d = _make_temp_dir()
        try:
            registry, store, cid, _, _ = _make_registry(Path(d))
            try:
                h1 = registry.get_or_create(cid)
                h2 = registry.get_or_create(cid)
                self.assertIs(h1, h2)
                self.assertEqual(len(registry), 1)
            finally:
                _close_and_cleanup(registry, store)
        finally:
            _safe_rmtree(d)

    def test_different_conversations_get_different_handles(self) -> None:
        d = _make_temp_dir()
        try:
            registry, store, cid_a, _, _ = _make_registry(Path(d))
            try:
                conv_b = store.create(project_id="p1", display_title="B", mode="goal")
                cid_b = conv_b.conversation_id
                h_a = registry.get_or_create(cid_a)
                h_b = registry.get_or_create(cid_b)
                self.assertIsNot(h_a, h_b)
                self.assertEqual(len(registry), 2)
            finally:
                _close_and_cleanup(registry, store)
        finally:
            _safe_rmtree(d)

    def test_get_returns_none_for_unknown_conversation(self) -> None:
        d = _make_temp_dir()
        try:
            registry, store, _, _, _ = _make_registry(Path(d))
            try:
                self.assertIsNone(registry.get("nonexistent"))
                self.assertNotIn("nonexistent", registry)
            finally:
                _close_and_cleanup(registry, store)
        finally:
            _safe_rmtree(d)


class ThreadSafetyTests(unittest.TestCase):
    def test_concurrent_get_or_create_same_conversation_returns_same_handle(self) -> None:
        d = _make_temp_dir()
        try:
            registry, store, cid, _, _ = _make_registry(Path(d))
            try:
                handles: list = []
                barrier = threading.Barrier(8)

                def worker() -> None:
                    barrier.wait()
                    handles.append(registry.get_or_create(cid))

                threads = [threading.Thread(target=worker) for _ in range(8)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=2.0)

                self.assertEqual(len(handles), 8)
                self.assertTrue(all(h is handles[0] for h in handles))
                self.assertEqual(len(registry), 1)
            finally:
                _close_and_cleanup(registry, store)
        finally:
            _safe_rmtree(d)


class EndToEndConversationLoopTests(unittest.TestCase):
    def test_followup_run_writeback_then_followup_again_reuses_handle(self) -> None:
        d = _make_temp_dir()
        try:
            registry, store, cid, orchestrated, factory_calls = _make_registry(
                Path(d),
                orchestrated=FakeOrchestrated(run_outcomes=[FakeResult(status="PASSED", verdict="第一轮完成"), FakeResult(status="PASSED", verdict="第二轮完成")]),
            )
            try:
                handle = registry.get_or_create(cid)

                # 第一轮
                handle.followup(user_message("修复订单 bug"))
                handle.when_idle()
                self.assertEqual(len(orchestrated.run_calls), 1)
                self.assertEqual(factory_calls[0][0], "修复订单 bug")

                # 第二轮：同会话追问
                handle.followup(user_message("那折扣算了吗"))
                handle.when_idle()
                self.assertEqual(len(orchestrated.run_calls), 2)
                self.assertEqual(factory_calls[1][0], "那折扣算了吗")

                # 两轮结果都写回会话
                messages = store.messages(cid)
                summaries = [m for m in messages if m.kind == "task_summary"]
                self.assertEqual(len(summaries), 2)
                self.assertEqual(summaries[0].task_verdict, "第一轮完成")
                self.assertEqual(summaries[1].task_verdict, "第二轮完成")

                # handle 状态回到 idle
                self.assertEqual(handle.status, AgentStatus.IDLE)
            finally:
                _close_and_cleanup(registry, store)
        finally:
            _safe_rmtree(d)

    def test_followup_with_inject_does_not_trigger_extra_run(self) -> None:
        d = _make_temp_dir()
        try:
            registry, store, cid, orchestrated, _ = _make_registry(Path(d))
            try:
                handle = registry.get_or_create(cid)

                handle.inject(AgentMessage(content="项目规则：禁改测试目录", origin=MessageOrigin.SYSTEM))
                time.sleep(0.05)
                # 不应该触发 run
                self.assertEqual(len(orchestrated.run_calls), 0)

                # followup 后 inject 的上下文被本轮领走
                handle.followup(user_message("开始任务"))
                handle.when_idle()
                self.assertEqual(len(orchestrated.run_calls), 1)
            finally:
                _close_and_cleanup(registry, store)
        finally:
            _safe_rmtree(d)


class CancelAllAndWaitTests(unittest.TestCase):
    def test_cancel_all_and_wait_clears_all_handles(self) -> None:
        d = _make_temp_dir()
        try:
            registry, store, cid_a, _, _ = _make_registry(Path(d))
            try:
                conv_b = store.create(project_id="p1", display_title="B", mode="goal")
                cid_b = conv_b.conversation_id
                h_a = registry.get_or_create(cid_a)
                h_b = registry.get_or_create(cid_b)

                registry.cancel_all_and_wait(reason="test shutdown")

                self.assertEqual(h_a.status, AgentStatus.IDLE)
                self.assertEqual(h_b.status, AgentStatus.IDLE)
            finally:
                _close_and_cleanup(registry, store)
        finally:
            _safe_rmtree(d)

    def test_cancel_all_and_wait_handles_empty_registry_safely(self) -> None:
        d = _make_temp_dir()
        try:
            registry, store, _, _, _ = _make_registry(Path(d))
            try:
                registry.cancel_all_and_wait(reason="empty")
            finally:
                _close_and_cleanup(registry, store)
        finally:
            _safe_rmtree(d)


if __name__ == "__main__":
    unittest.main()
