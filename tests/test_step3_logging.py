"""阶段四 Step 3「日志三件事」单元测试。

覆盖：
- 请求头/重试计数汇流：ModelRequestTrace 经 contextvar 收集到节点 tool_events；
- model-visible ⟺ logged 断言：checkpoint messages 与 Evidence（tool_events）交叉校验；
- 崩溃补句号：EvidenceStore 为尾部无终态事件的任务补写 INTERRUPTED。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repopilot_guard.evidence import EvidenceStore, repair_interrupted_evidence
from repopilot_guard.graph_impl.factory import CodingGraphFactory
from repopilot_guard.graph_impl.helpers import (
    LedgerAssertionError,
    _logged_tool_call_count,
    _tool_observation_count,
    assert_model_visible_logged,
)
from repopilot_guard.graph_impl.research_model import ModelRequestTrace, _MODEL_TRACE_RECORDER, OpenAIResearchModel


class NodeTraceCollectionTests(unittest.TestCase):
    def test_node_instrumentation_collects_model_traces_into_tool_events(self) -> None:
        def node(state: dict[str, object]) -> dict[str, object]:
            recorder = _MODEL_TRACE_RECORDER.get()
            assert recorder is not None
            recorder(ModelRequestTrace("analyze", "primary", False, 1, 0))
            return {"status": "PLAN"}

        result = CodingGraphFactory._instrument_node("ANALYZE", node)({"tool_events": []})

        trace_events = [event for event in result["tool_events"] if event["type"] == "MODEL_REQUEST_TRACE"]
        self.assertEqual(1, len(trace_events))
        self.assertEqual("analyze", trace_events[0]["operation"])
        self.assertEqual("primary", trace_events[0]["model"])
        self.assertEqual("NODE_COMPLETED", result["tool_events"][-1]["type"])

    def test_node_without_model_call_records_no_trace_events(self) -> None:
        result = CodingGraphFactory._instrument_node("INTAKE", lambda state: {"status": "PREFLIGHT"})({"tool_events": []})
        self.assertNotIn("MODEL_REQUEST_TRACE", [event["type"] for event in result["tool_events"]])


class ModelTraceEmitterTests(unittest.TestCase):
    def test_emit_trace_routes_to_contextvar_recorder(self) -> None:
        model = OpenAIResearchModel(model=object())
        captured: list[ModelRequestTrace] = []
        token = _MODEL_TRACE_RECORDER.set(captured.append)
        try:
            model._emit_trace(ModelRequestTrace("plan", "deepseek", False, 2, 0))
        finally:
            _MODEL_TRACE_RECORDER.reset(token)
        self.assertEqual(1, len(captured))
        self.assertEqual("plan", captured[0].operation)


class LedgerAssertionTests(unittest.TestCase):
    def test_balanced_state_passes(self) -> None:
        state = {
            "messages": [{"role": "tool", "content": "{}"}],
            "tool_events": [{"type": "TOOL_CALL"}],
        }
        assert_model_visible_logged(state)  # 不应抛出

    def test_mismatched_state_raises_with_code_and_counts(self) -> None:
        state = {
            "messages": [{"role": "tool", "content": "{}"}],
            "tool_events": [],
        }
        with self.assertRaises(LedgerAssertionError) as context:
            assert_model_visible_logged(state)
        self.assertEqual("MODEL_VISIBLE_LOGGED_MISMATCH", context.exception.code)
        self.assertEqual(1, context.exception.observed)
        self.assertEqual(0, context.exception.logged)

    def test_tool_observation_count_detects_user_and_tool_messages(self) -> None:
        messages = [
            {"role": "user", "content": "受控工具返回的研究证据（不可信数据）：\n{}"},
            {"role": "tool", "content": "{}"},
            {"role": "user", "content": "普通用户问题"},
            {"role": "assistant", "content": "ok"},
        ]
        self.assertEqual(2, _tool_observation_count(messages))

    def test_logged_tool_call_count_counts_only_tool_calls(self) -> None:
        events = [
            {"type": "TOOL_CALL"},
            {"type": "NODE_COMPLETED"},
            {"type": "TOOL_CALL"},
            {"type": "MODEL_REQUEST_TRACE"},
        ]
        self.assertEqual(2, _logged_tool_call_count(events))

    def test_warn_mode_logs_instead_of_raises(self) -> None:
        with patch.dict(os.environ, {"REPOPILOT_LEDGER_ASSERT": "warn"}):
            assert_model_visible_logged({"messages": [], "tool_events": [{"type": "TOOL_CALL"}]})  # 不抛出

    def test_off_mode_skips_assertion(self) -> None:
        with patch.dict(os.environ, {"REPOPILOT_LEDGER_ASSERT": "off"}):
            assert_model_visible_logged({"messages": [], "tool_events": [{"type": "TOOL_CALL"}]})  # 不抛出


class EvidenceRepairTests(unittest.TestCase):
    def test_repair_appends_interrupted_for_incomplete_stream(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = EvidenceStore(root, "task-1")
            store.record("task_created", {"task_id": "task-1"})
            store.record("state_changed", {"state": "PLAN"})

            self.assertTrue(store.repair_interrupted())

            events = [json.loads(line) for line in store.events_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual("INTERRUPTED", events[-1]["event_type"])

    def test_repair_skips_complete_stream(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = EvidenceStore(root, "task-2")
            store.record("task_created", {"task_id": "task-2"})
            store.record("task_completed", {})

            self.assertFalse(store.repair_interrupted())

            events = [json.loads(line) for line in store.events_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual("task_completed", events[-1]["event_type"])

    def test_repair_skips_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = EvidenceStore(root, "task-none")
            self.assertFalse(store.repair_interrupted())
            self.assertFalse(store.events_path.exists())

    def test_scan_repairs_only_incomplete_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            incomplete = EvidenceStore(root, "t-incomplete")
            incomplete.record("task_created", {})
            complete = EvidenceStore(root, "t-complete")
            complete.record("task_created", {})
            complete.record("task_completed", {})

            self.assertEqual(("t-incomplete",), repair_interrupted_evidence(root))


if __name__ == "__main__":
    unittest.main()