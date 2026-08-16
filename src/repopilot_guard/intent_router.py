"""将自然语言请求路由到对话、项目问答、只读研究或受控修改。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from repopilot_guard.providers import ChatModelProvider, OpenAICompatibleProvider


class IntentRouteName(StrEnum):
    """路由只描述建议的工作流，不能表达权限或工具授权。"""

    CHAT = "chat"
    PROJECT_QA = "project_qa"
    CODE_RESEARCH = "code_research"
    CODE_CHANGE = "code_change"


@dataclass(frozen=True, slots=True)
class IntentRoute:
    """可投影到桌面端的受限路由结果。"""

    intent: IntentRouteName
    confidence: float
    reason: str
    source: str

    @property
    def requires_confirmation(self) -> bool:
        """低置信度只请求用户确认，不会自动启动代码任务。"""

        return self.confidence < 0.72

    def to_dict(self) -> dict[str, object]:
        return {
            "intent": self.intent.value,
            "confidence": round(self.confidence, 2),
            "reason": self.reason,
            "source": self.source,
            "requires_confirmation": self.requires_confirmation,
        }


class _ChatModel(Protocol):
    def invoke(self, messages: list[dict[str, str]]) -> object: ...


class IntentRouter:
    """模型优先、规则兜底的意图路由器；模型输出不能越权。"""

    def __init__(self, provider: ChatModelProvider) -> None:
        self._provider = provider

    @classmethod
    def from_environment(cls) -> "IntentRouter":
        from repopilot_guard.config import AppSettings

        return cls(OpenAICompatibleProvider(AppSettings()))

    def route(self, content: str, *, has_project: bool) -> IntentRoute:
        fallback = self._rule_fallback(content, has_project=has_project)
        try:
            chat_check = getattr(self._provider, "chat_check", self._provider.check)
            readiness = chat_check()
            if not readiness.ready:
                return fallback
            response = self._provider.create_chat_model().invoke(self._messages(content, has_project=has_project))
            parsed = self._parse_response(_message_content(response))
            if parsed is None:
                return fallback
            return self._validated_model_route(parsed, has_project=has_project, fallback=fallback)
        except Exception:
            # 路由失败只能回退到明确、无副作用的规则，不允许阻断普通对话。
            return fallback

    @staticmethod
    def _messages(content: str, *, has_project: bool) -> list[dict[str, str]]:
        return [
            {
                "role": "system",
                "content": (
                    "你是 RepoPilot 的意图路由器。只判断用户请求应走哪个流程，"
                    "绝不能授予权限、调用工具、读取文件或执行命令。"
                    "仅输出一个 JSON 对象，不要 Markdown。"
                    "字段为 intent、confidence、reason。intent 只能是 chat、project_qa、"
                    "code_research、code_change。confidence 是 0 到 1 的数字。"
                    "chat：通用知识或闲聊；project_qa：介绍/概览当前项目，不要求定位具体实现；"
                    "code_research：定位、分析、评估仓库代码且不要求改动；当用户要求详细介绍、项目流程、模块关系、调用链、目录结构"
                    "或明确要求结合代码时，即使没有说“分析”，也应选择 code_research；"
                    "code_change：明确要求修复、修改、新增、删除或执行代码操作。"
                    "询问“如何/怎么/为什么实现、原理、原因、在哪”等，即使提到“实现、优化、异常”，"
                    "也应选 code_research 而非 code_change。"
                    "不确定时选 chat 且给出低置信度。"
                ),
            },
            {
                "role": "user",
                "content": f"当前是否已选择项目：{'是' if has_project else '否'}\n用户输入：{content[:12_000]}",
            },
        ]

    @staticmethod
    def _parse_response(content: str) -> dict[str, object] | None:
        candidate = content.strip()
        if not candidate:
            return None
        if not candidate.startswith("{"):
            match = re.search(r"\{.*\}", candidate, flags=re.DOTALL)
            if match is None:
                return None
            candidate = match.group(0)
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _validated_model_route(
        payload: dict[str, object],
        *,
        has_project: bool,
        fallback: IntentRoute,
    ) -> IntentRoute:
        raw_intent = payload.get("intent")
        try:
            intent = IntentRouteName(str(raw_intent))
        except ValueError:
            return fallback
        raw_confidence = payload.get("confidence")
        try:
            confidence = float(raw_confidence)
        except (TypeError, ValueError):
            return fallback
        if not 0 <= confidence <= 1:
            return fallback
        reason = str(payload.get("reason") or "模型未提供路由理由").strip().replace("\n", " ")[:240]
        if not reason:
            reason = "模型未提供路由理由"
        if not has_project and intent in {IntentRouteName.PROJECT_QA, IntentRouteName.CODE_RESEARCH, IntentRouteName.CODE_CHANGE}:
            return IntentRoute(
                IntentRouteName.CHAT,
                0.95,
                "该请求需要项目上下文；请先选择本地项目后再继续。",
                "project_required",
            )
        return IntentRoute(intent, confidence, reason, "model")

    @staticmethod
    def _rule_fallback(content: str, *, has_project: bool) -> IntentRoute:
        normalized = content.strip().lower()
        if not has_project and re.search(r"项目|仓库|代码|bug|修复|修改|文件|函数|类|接口|模块|实现", normalized):
            return IntentRoute(
                IntentRouteName.CHAT,
                0.95,
                "该请求需要项目上下文；请先选择本地项目后再继续。",
                "rule_fallback",
            )
        # 只读研究/咨询意图优先于"实现/优化/异常"等可能被误解为修改的动词：
        # 用户问"如何/为什么/在哪/原理"时，即使提到"实现/优化"，也是想理解而非改动。
        if re.search(
            r"如何|怎么|为什么|为何|原因|原理|是什么|在哪|哪里|哪些|分析|解释|讲解|梳理|排查|评估|定位|查找|搜索|检索|调用链|依赖|架构|结构|设计|流程|结合代码|详细|模块关系|目录结构",
            normalized,
        ):
            return IntentRoute(IntentRouteName.CODE_RESEARCH, 0.86, "请求需要基于仓库代码给出可定位的详细结论。", "rule_fallback")
        if re.search(r"介绍.*项目|项目.*介绍|项目概览|技术栈|这个仓库|这是什么项目", normalized):
            return IntentRoute(IntentRouteName.PROJECT_QA, 0.8, "请求是当前项目的概览问答。", "rule_fallback")
        if re.search(
            r"修复|修改|改正|修正|新增|添加|增加|删除|移除|重构|改写|重写|实现|补全|加上|生成|创建|写一个|改一下|优化|调整|\bfix\b|bug|错误|异常|失效|不生效",
            normalized,
        ):
            return IntentRoute(IntentRouteName.CODE_CHANGE, 0.86, "请求明确包含代码改动目标。", "rule_fallback")
        return IntentRoute(IntentRouteName.CHAT, 0.6, "未能从文字中明确判断是否需要项目代码上下文。", "rule_fallback")


def _message_content(response: object) -> str:
    """兼容 LangChain 消息对象与测试替身，且不接受工具调用内容。"""

    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(item for item in content if isinstance(item, str))
    return ""
