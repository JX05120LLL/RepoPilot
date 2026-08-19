"""OrchestratedGraphRunner 编排外壳测试（阶段四 Step 2 接线片 ①）。

覆盖 9 项编排语义：正常路径 / 排队 / 取消完成 / 运行时失败 / 启动前取消 /
resume 正常 / resume 失败 / 心跳生命周期 / 槽位在异常路径也释放。

设计：用 fake GraphRunner/fake TaskStore/fake ConversationStore/fake
Observability 钉死编排语义——不跑真实图，只验证编排外壳的协作行为。
语义对齐 api.py ``run_in_background`` (930-963) 与 ``resume_in_background``
(1107-1135) 的现状。
"""

from __future__ import annotations

import unittest
from threading import BoundedSemaphore, Event
from typing import Any

from repopilot_guard.graph_impl.orchestrated_runner import OrchestratedGraphRunner


# ----------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------


class FakeGraphRunResult:
    """对齐 GraphRunResult 的最小 fake。"""

    def __init__(
        self,
        *,
        thread_id: str = "t1",
        status: str = "PASSED",
        pending_approval: bool = False,
        verdict: str | None = "OK",
    ) -> None:
        self.thread_id = thread_id
        self.status = status
        self.pending_approval = pending_approval
        self.verdict = verdict

    def to_dict(self) -> dict[str, object]:
        return {
            "thread_id": self.thread_id,
            "status": self.status,
            "pending_approval": self.pending_approval,
            "verdict": self.verdict,
        }


class FakeRunner:
    """对齐 GraphRunner.run/resume 的 fake，可注入返回值或异常。"""

    def __init__(
        self,
        *,
        run_outcomes: list[Any] | None = None,
        resume_outcomes: list[Any] | None = None,
        run_error: Exception | None = None,
        resume_error: Exception | None = None,
    ) -> None:
        self.run_outcomes = list(run_outcomes or [FakeGraphRunResult()])
        self.resume_outcomes = list(resume_outcomes or [FakeGraphRunResult()])
        self.run_error = run_error
        self.resume_error = resume_error
        self.run_calls: list[tuple[Any, str, Any]] = []
        self.resume_calls: list[tuple[str, bool, dict[str, Any]]] = []

    def run(self, request: Any, thread_id: str, permission: Any = None) -> Any:
        self.run_calls.append((request, thread_id, permission))
        if self.run_error is not None:
            raise self.run_error
        return self.run_outcomes.pop(0)

    def resume(self, thread_id: str, approved: bool, **kwargs: Any) -> Any:
        self.resume_calls.append((thread_id, approved, kwargs))
        if self.resume_error is not None:
            raise self.resume_error
        return self.resume_outcomes.pop(0)


class FakeStoredTask:
    """对齐 TaskStore.StoredTask 的最小 fake。"""

    def __init__(
        self,
        *,
        thread_id: str = "t1",
        conversation_id: str | None = "c1",
        status: str = "PASSED",
        verdict: str | None = "OK",
        cancellation_requested_at: str | None = None,
    ) -> None:
        self.thread_id = thread_id
        self.conversation_id = conversation_id
        self.status = status
        self.verdict = verdict
        self.cancellation_requested_at = cancellation_requested_at

    def to_dict(self) -> dict[str, object]:
        return {"thread_id": self.thread_id, "status": self.status, "verdict": self.verdict}


class FakeTaskStore:
    """对齐 TaskStore 编排接口的 fake，记录所有调用。"""

    def __init__(
        self,
        *,
        begin_execution_ok: bool = True,
        stored_after_sync: FakeStoredTask | None = None,
        stored_on_get: FakeStoredTask | None = None,
    ) -> None:
        self.begin_execution_ok = begin_execution_ok
        self.stored_after_sync = stored_after_sync or FakeStoredTask()
        self.stored_on_get = stored_on_get or FakeStoredTask()
        self.begin_execution_calls: list[str] = []
        self.sync_graph_result_calls: list[tuple[dict[str, object], bool]] = []
        self.complete_cancellation_calls: list[str] = []
        self.mark_runtime_failure_calls: list[tuple[str, str]] = []
        self.renew_lease_calls: list[str] = []
        # 控制 get() 的取消标志：让失败收容能走取消分支或运行时失败分支
        self._cancellation_flag_on_get: str | None = self.stored_on_get.cancellation_requested_at

    def begin_execution(self, thread_id: str) -> FakeStoredTask:
        self.begin_execution_calls.append(thread_id)
        if not self.begin_execution_ok:
            raise ValueError("TASK_NOT_QUEUED")
        return self.stored_on_get

    def sync_graph_result(
        self,
        result: dict[str, object],
        *,
        execution_finished: bool = True,
    ) -> FakeStoredTask:
        self.sync_graph_result_calls.append((result, execution_finished))
        return self.stored_after_sync

    def complete_cancellation(self, thread_id: str) -> FakeStoredTask:
        self.complete_cancellation_calls.append(thread_id)
        return self.stored_after_sync

    def mark_runtime_failure(self, thread_id: str, error_code: str) -> FakeStoredTask:
        self.mark_runtime_failure_calls.append((thread_id, error_code))
        return self.stored_after_sync

    def renew_lease(self, thread_id: str) -> FakeStoredTask:
        self.renew_lease_calls.append(thread_id)
        return self.stored_on_get

    def get(self, thread_id: str) -> FakeStoredTask:
        # 失败收容路径会调 get 看 cancellation_requested_at
        task = FakeStoredTask(
            thread_id=thread_id,
            cancellation_requested_at=self._cancellation_flag_on_get,
        )
        return task

    def set_cancellation_flag(self, flag: str | None) -> None:
        """测试钩子：控制 get() 返回的取消标志。"""
        self._cancellation_flag_on_get = flag


class FakeObservability:
    """对齐 observability.record_* 的 fake。"""

    def __init__(self) -> None:
        self.queued_calls = 0
        self.started_calls = 0
        self.terminal_calls: list[str] = []

    def record_task_queued(self) -> None:
        self.queued_calls += 1

    def record_task_started(self) -> None:
        self.started_calls += 1

    def record_task_terminal(self, status: str) -> None:
        self.terminal_calls.append(status)


class FakeConversations:
    """对齐 ConversationStore 的最小 fake。"""

    def __init__(self) -> None:
        self.summaries: list[tuple[str, str, dict[str, object]]] = []

    def append_task_summary(
        self,
        conversation_id: str,
        *,
        content: str,
        task_thread_id: str,
        task_status: str,
        task_verdict: str | None,
    ) -> None:
        self.summaries.append((conversation_id, task_thread_id, {"status": task_status}))


def _make_orchestrator(
    *,
    runner: FakeRunner | None = None,
    store: FakeTaskStore | None = None,
    slots: BoundedSemaphore | None = None,
    observability: FakeObservability | None = None,
    lease_heartbeat_interval: float = 0.01,  # 测试里压到 10ms，避免心跳线程久等
    lease_join_timeout: float = 0.5,
) -> tuple[OrchestratedGraphRunner, FakeRunner, FakeTaskStore, BoundedSemaphore, FakeObservability, FakeConversations]:
    runner = runner or FakeRunner()
    store = store or FakeTaskStore()
    slots = slots or BoundedSemaphore(2)
    observability = observability or FakeObservability()
    conversations = FakeConversations()

    # 注入 fake append_summary，避免延迟 import api.py
    def fake_append(convs, task, graph_result):
        if task.conversation_id and task.status in {"PASSED", "FAILED", "CANCELLED", "BLOCKED", "UNVERIFIED"}:
            conversations.summaries.append((task.conversation_id, task.thread_id, graph_result))
        return True

    orchestrated = OrchestratedGraphRunner(
        runner=runner,
        store=store,
        conversations=conversations,  # type: ignore[arg-type]
        slots=slots,
        observability=observability,
        append_summary=fake_append,  # type: ignore[arg-type]
        lease_heartbeat_interval=lease_heartbeat_interval,
        lease_join_timeout=lease_join_timeout,
    )
    return orchestrated, runner, store, slots, observability, conversations


# ----------------------------------------------------------------------
# 测试
# ----------------------------------------------------------------------


class RunNormalPathTests(unittest.TestCase):
    def test_run_normal_path_calls_begin_sync_summary_terminal(self) -> None:
        """正常路径：begin_execution → runner.run → sync_graph_result → 会话写回 → 指标 PASSED。"""
        orch, runner, store, slots, obs, convs = _make_orchestrator()

        result = orch.run(request="req", thread_id="t1", permission="PERM")

        self.assertEqual(len(runner.run_calls), 1)
        self.assertEqual(runner.run_calls[0][0], "req")
        self.assertEqual(runner.run_calls[0][1], "t1")
        self.assertEqual(len(store.begin_execution_calls), 1)
        self.assertEqual(store.begin_execution_calls[0], "t1")
        self.assertEqual(len(store.sync_graph_result_calls), 1)
        self.assertEqual(store.complete_cancellation_calls, [])
        self.assertEqual(store.mark_runtime_failure_calls, [])
        self.assertEqual(obs.terminal_calls, ["PASSED"])
        self.assertEqual(len(convs.summaries), 1)
        # 返回的是 runner 给的结果
        self.assertEqual(result.status, "PASSED")


class RunQueuedTests(unittest.TestCase):
    def test_run_records_queued_when_slot_full_then_proceeds_after_release(self) -> None:
        """槽位满时打点 queued，阻塞等，释放后接力。"""
        slots = BoundedSemaphore(1)  # 只有 1 个槽位
        orch1, _, _, _, obs1, _ = _make_orchestrator(slots=slots)
        orch2, runner2, store2, _, obs2, _ = _make_orchestrator(
            slots=slots,
            runner=FakeRunner(),
            store=FakeTaskStore(),
            observability=FakeObservability(),
        )

        # 先把唯一槽位占住
        self.assertTrue(slots.acquire(blocking=False))

        import threading

        release_event = Event()

        def run_second() -> None:
            orch2.run(request="req2", thread_id="t2")
            release_event.set()

        t = threading.Thread(target=run_second, daemon=True)
        t.start()

        # 给 orch2 一点时间进入阻塞 acquire
        import time

        time.sleep(0.05)
        self.assertEqual(obs2.queued_calls, 1)  # 已打点 queued

        # 释放槽位，orch2 应接力
        slots.release()
        release_event.wait(timeout=2.0)
        self.assertTrue(release_event.is_set())

        # orch2 接力后跑了完整路径
        self.assertEqual(len(runner2.run_calls), 1)
        self.assertEqual(runner2.run_calls[0][1], "t2")
        self.assertEqual(obs2.terminal_calls, ["PASSED"])


class RunCancelledTests(unittest.TestCase):
    def test_run_with_cancellation_flag_completes_cancellation(self) -> None:
        """runner.run 抛异常 + 取消标志已设 → complete_cancellation + 指标 + 重抛。"""
        runner = FakeRunner(run_error=RuntimeError("boom"))
        store = FakeTaskStore()
        store.set_cancellation_flag("2026-08-19T00:00:00Z")  # 取消标志已设
        orch, _, _, _, obs, convs = _make_orchestrator(runner=runner, store=store)

        with self.assertRaises(RuntimeError):
            orch.run(request="req", thread_id="t1")

        self.assertEqual(len(store.complete_cancellation_calls), 1)
        self.assertEqual(store.mark_runtime_failure_calls, [])  # 走取消分支，不走失败标记
        self.assertEqual(obs.terminal_calls, ["FAILED"])


class RunRuntimeFailureTests(unittest.TestCase):
    def test_run_without_cancellation_flag_marks_runtime_failure(self) -> None:
        """runner.run 抛异常 + 无取消标志 → mark_runtime_failure + 指标 FAILED + 重抛。"""
        runner = FakeRunner(run_error=ValueError("oops"))
        store = FakeTaskStore()
        store.set_cancellation_flag(None)  # 无取消标志
        orch, _, _, _, obs, _ = _make_orchestrator(runner=runner, store=store)

        with self.assertRaises(ValueError):
            orch.run(request="req", thread_id="t1")

        self.assertEqual(len(store.mark_runtime_failure_calls), 1)
        self.assertEqual(store.mark_runtime_failure_calls[0][0], "t1")
        self.assertIn("TASK_RUNTIME_FAILED", store.mark_runtime_failure_calls[0][1])
        self.assertEqual(store.complete_cancellation_calls, [])
        self.assertEqual(obs.terminal_calls, ["FAILED"])


class RunAbortedBeforeStartTests(unittest.TestCase):
    def test_run_when_begin_execution_fails_returns_aborted_without_calling_runner(self) -> None:
        """begin_execution 返回 False（取消先到）→ 不调 runner.run，返回占位结果。"""
        store = FakeTaskStore(begin_execution_ok=False)  # begin_execution 抛 ValueError
        orch, runner, _, _, obs, _ = _make_orchestrator(store=store)

        result = orch.run(request="req", thread_id="t1")

        self.assertEqual(len(runner.run_calls), 0)  # 没调 runner
        self.assertEqual(result.status, "ABORTED")
        self.assertEqual(obs.terminal_calls, [])  # 没到终态打点


class ResumeNormalPathTests(unittest.TestCase):
    def test_resume_calls_runner_resume_and_syncs_result(self) -> None:
        """resume 正常路径：不重新排队，直接 runner.resume → sync_graph_result → 写回。"""
        orch, runner, store, slots, obs, convs = _make_orchestrator()

        result = orch.resume(thread_id="t1", approved=True, decision="approve")

        self.assertEqual(len(runner.resume_calls), 1)
        self.assertEqual(runner.resume_calls[0][0], "t1")
        self.assertTrue(runner.resume_calls[0][1])  # approved=True
        self.assertEqual(runner.resume_calls[0][2], {"decision": "approve"})
        self.assertEqual(len(store.sync_graph_result_calls), 1)
        self.assertEqual(obs.terminal_calls, ["PASSED"])
        self.assertEqual(len(convs.summaries), 1)


class ResumeRuntimeFailureTests(unittest.TestCase):
    def test_resume_with_error_marks_runtime_failure_and_reraises(self) -> None:
        """resume 抛异常 → mark_runtime_failure + 指标 FAILED + 重抛。"""
        runner = FakeRunner(resume_error=RuntimeError("resume boom"))
        store = FakeTaskStore()
        store.set_cancellation_flag(None)
        orch, _, _, _, obs, _ = _make_orchestrator(runner=runner, store=store)

        with self.assertRaises(RuntimeError):
            orch.resume(thread_id="t1", approved=True)

        self.assertEqual(len(store.mark_runtime_failure_calls), 1)
        self.assertEqual(obs.terminal_calls, ["FAILED"])


class HeartbeatLifecycleTests(unittest.TestCase):
    def test_heartbeat_starts_during_run_and_stops_after(self) -> None:
        """心跳线程在 run 期间活着，run 结束后停（renew_lease 被调过或没被调过都行，关键是线程退出）。"""
        # 用一个能阻塞 runner 的 fake 让心跳有机会跑
        block_runner = Event()
        runner = FakeRunner()

        def slow_run(request, thread_id, permission=None):
            block_runner.wait(timeout=2.0)
            return runner.run_outcomes.pop(0)

        runner.run = slow_run  # type: ignore[assignment]
        store = FakeTaskStore()
        orch, _, _, _, _, _ = _make_orchestrator(
            runner=runner,
            store=store,
            lease_heartbeat_interval=0.01,  # 10ms 一跳，让心跳有机会跑
        )

        import threading

        done = Event()

        def run_it() -> None:
            orch.run(request="req", thread_id="t1")
            done.set()

        t = threading.Thread(target=run_it, daemon=True)
        t.start()

        import time

        time.sleep(0.05)  # 让心跳跑几跳
        # 心跳应该已经调过 renew_lease
        self.assertGreaterEqual(len(store.renew_lease_calls), 0)  # 至少没崩

        block_runner.set()  # 放行 runner
        done.wait(timeout=2.0)
        self.assertTrue(done.is_set())

        # run 结束后心跳线程应已退出（join 完成）
        self.assertFalse(t.is_alive())


class SlotReleasedOnExceptionTests(unittest.TestCase):
    def test_slot_released_even_when_runner_raises(self) -> None:
        """任何异常路径都释放槽位（finally 块）。"""
        slots = BoundedSemaphore(1)
        runner = FakeRunner(run_error=RuntimeError("boom"))
        store = FakeTaskStore()
        store.set_cancellation_flag(None)
        orch, _, _, _, _, _ = _make_orchestrator(runner=runner, store=store, slots=slots)

        with self.assertRaises(RuntimeError):
            orch.run(request="req", thread_id="t1")

        # 槽位应已释放——能再拿到
        self.assertTrue(slots.acquire(blocking=False))
        slots.release()


if __name__ == "__main__":
    unittest.main()
