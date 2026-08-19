"""阶段四 Step 3：模型请求轨迹（provider/model、降级、重试计数）捕获测试。

验证 OpenAIResearchModel 在成功 / 降级 / 无备选耗尽三种路径下，
经 request_trace_sink 产出正确的 ModelRequestTrace（只记事实、不含密钥）。
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx
from openai import APIConnectionError

from repopilot_guard.graph_impl.research_model import ModelRequestTrace, OpenAIResearchModel


class _OkModel:
    def bind_tools(self, tools: list[object]) -> "_OkModel":
        return self

    def bind(self, **kwargs: object) -> "_OkModel":
        return self

    def invoke(self, messages: list[dict[str, str]]) -> object:
        return type("Response", (), {"content": "ok", "tool_calls": []})()


class _AlwaysFailsModel:
    def __init__(self) -> None:
        self.calls = 0

    def bind_tools(self, tools: list[object]) -> "_AlwaysFailsModel":
        return self

    def bind(self, **kwargs: object) -> "_AlwaysFailsModel":
        return self

    def invoke(self, messages: list[dict[str, str]]) -> object:
        self.calls += 1
        raise APIConnectionError(request=httpx.Request("POST", "https://chat.invalid"))


def _make(primary, fallback, sink) -> OpenAIResearchModel:
    return OpenAIResearchModel(
        model=primary,
        fallback_model=fallback,
        model_label="deepseek-primary",
        fallback_label="deepseek-fallback",
        request_trace_sink=sink,
    )


class ModelRequestTraceTests(unittest.TestCase):
    def test_primary_success_records_single_attempt_no_fallback(self) -> None:
        traces: list[ModelRequestTrace] = []
        model = _make(_OkModel(), _OkModel(), traces.append)

        with patch("repopilot_guard.graph_impl.research_model.time.sleep"):
            model.analyze([], ())

        self.assertEqual(len(traces), 1)
        trace = traces[0]
        self.assertEqual(trace.operation, "analyze")
        self.assertEqual(trace.model_label, "deepseek-primary")
        self.assertFalse(trace.used_fallback)
        self.assertEqual(trace.primary_attempts, 1)
        self.assertEqual(trace.fallback_attempts, 0)

    def test_fallback_path_records_degradation_and_retry_counts(self) -> None:
        traces: list[ModelRequestTrace] = []
        primary = _AlwaysFailsModel()
        fallback = _OkModel()
        model = _make(primary, fallback, traces.append)

        with patch("repopilot_guard.graph_impl.research_model.time.sleep"):
            model.analyze([], ())

        self.assertEqual(len(traces), 1)
        trace = traces[0]
        self.assertTrue(trace.used_fallback)
        self.assertEqual(trace.model_label, "deepseek-fallback")
        self.assertEqual(trace.primary_attempts, 3, "主模型有界重试耗尽")
        self.assertEqual(trace.fallback_attempts, 1)
        self.assertEqual(primary.calls, 3)

    def test_no_fallback_exhaustion_emits_trace_then_raises(self) -> None:
        traces: list[ModelRequestTrace] = []
        primary = _AlwaysFailsModel()
        model = _make(primary, None, traces.append)

        with patch("repopilot_guard.graph_impl.research_model.time.sleep"):
            with self.assertRaises(APIConnectionError):
                model.analyze([], ())

        self.assertEqual(len(traces), 1)
        trace = traces[0]
        self.assertFalse(trace.used_fallback)
        self.assertEqual(trace.primary_attempts, 3)

    def test_trace_to_dict_is_fact_only(self) -> None:
        trace = ModelRequestTrace("plan", "deepseek-primary", False, 2, 0)
        payload = trace.to_dict()
        self.assertEqual(payload["type"], "MODEL_REQUEST_TRACE")
        self.assertEqual(payload["operation"], "plan")
        self.assertEqual(payload["model"], "deepseek-primary")
        self.assertFalse(payload["used_fallback"])
        self.assertEqual(payload["primary_attempts"], 2)
        self.assertEqual(payload["fallback_attempts"], 0)


if __name__ == "__main__":
    unittest.main()
