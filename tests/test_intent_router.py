from __future__ import annotations

import unittest
from dataclasses import dataclass

from repopilot_guard.config import ComponentCheck
from repopilot_guard.intent_router import IntentRouteName, IntentRouter


@dataclass
class FakeMessage:
    content: str


class FakeModel:
    def __init__(self, response: str) -> None:
        self.response = response
        self.messages: list[dict[str, str]] = []

    def invoke(self, messages: list[dict[str, str]]) -> FakeMessage:
        self.messages = messages
        return FakeMessage(self.response)


class FakeProvider:
    def __init__(self, response: str, *, ready: bool = True) -> None:
        self.model = FakeModel(response)
        self.ready = ready

    def check(self) -> ComponentCheck:
        return self.chat_check()

    def chat_check(self) -> ComponentCheck:
        return ComponentCheck("chat_provider", self.ready, "READY" if self.ready else "CHAT_MISSING", "test")

    def create_chat_model(self) -> FakeModel:
        return self.model


class IntentRouterTests(unittest.TestCase):
    def test_model_returns_structured_project_question_route(self) -> None:
        router = IntentRouter(FakeProvider('{"intent":"project_qa","confidence":0.94,"reason":"请求项目概览"}'))

        result = router.route("介绍一下这个项目", has_project=True)

        self.assertEqual(IntentRouteName.PROJECT_QA, result.intent)
        self.assertEqual(0.94, result.confidence)
        self.assertEqual("model", result.source)
        self.assertFalse(result.requires_confirmation)

    def test_invalid_model_output_falls_back_without_blocking_chat(self) -> None:
        router = IntentRouter(FakeProvider("not json"))

        result = router.route("你好", has_project=False)

        self.assertEqual(IntentRouteName.CHAT, result.intent)
        self.assertEqual("rule_fallback", result.source)
        self.assertTrue(result.requires_confirmation)

    def test_code_change_requires_project_even_when_model_requests_it(self) -> None:
        router = IntentRouter(FakeProvider('{"intent":"code_change","confidence":0.99,"reason":"修复 bug"}'))

        result = router.route("修复订单地址校验 bug", has_project=False)

        self.assertEqual(IntentRouteName.CHAT, result.intent)
        self.assertEqual("project_required", result.source)

    def test_unavailable_model_uses_safe_rule_fallback(self) -> None:
        router = IntentRouter(FakeProvider("{}", ready=False))

        result = router.route("定位订单地址校验逻辑", has_project=True)

        self.assertEqual(IntentRouteName.CODE_RESEARCH, result.intent)
        self.assertEqual("rule_fallback", result.source)

    def test_detailed_project_introduction_uses_read_only_code_research(self) -> None:
        router = IntentRouter(FakeProvider("{}", ready=False))

        result = router.route("请结合代码详细介绍这个项目的模块和流程", has_project=True)

        self.assertEqual(IntentRouteName.CODE_RESEARCH, result.intent)
        self.assertEqual("rule_fallback", result.source)
