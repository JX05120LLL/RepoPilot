"""阶段 2（2.3）模型降级：主模型瞬态故障耗尽后自动切换到备选模型。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx
from openai import APIConnectionError

from repopilot_guard.graph import OpenAIResearchModel


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


class _FallbackModel:
    def __init__(self) -> None:
        self.calls = 0

    def bind_tools(self, tools: list[object]) -> "_FallbackModel":
        return self

    def bind(self, **kwargs: object) -> "_FallbackModel":
        return self

    def invoke(self, messages: list[dict[str, str]]) -> object:
        self.calls += 1
        return type("Response", (), {"content": "降级成功", "tool_calls": []})()


class ModelFallbackTests(unittest.TestCase):
    def test_analyze_falls_back_after_primary_transient_failures(self) -> None:
        primary = _AlwaysFailsModel()
        fallback = _FallbackModel()
        model = OpenAIResearchModel(model=primary, fallback_model=fallback)

        with patch("repopilot_guard.graph.time.sleep"):
            result = model.analyze([], ())

        self.assertEqual("降级成功", result.content)
        self.assertEqual(3, primary.calls)
        self.assertEqual(1, fallback.calls)

    def test_invoke_json_falls_back_after_primary_transient_failures(self) -> None:
        primary = _AlwaysFailsModel()
        fallback = _FallbackModel()
        model = OpenAIResearchModel(model=primary, fallback_model=fallback)

        with patch("repopilot_guard.graph.time.sleep"):
            response = model._invoke_json([{"role": "user", "content": "生成 JSON"}])

        self.assertEqual("降级成功", response.content)
        self.assertEqual(3, primary.calls)
        self.assertEqual(1, fallback.calls)

    def test_no_fallback_reraises_transient_error(self) -> None:
        primary = _AlwaysFailsModel()
        model = OpenAIResearchModel(model=primary, fallback_model=None)

        with patch("repopilot_guard.graph.time.sleep"):
            with self.assertRaises(APIConnectionError):
                model.analyze([], ())

        self.assertEqual(3, primary.calls)


if __name__ == "__main__":
    unittest.main()
