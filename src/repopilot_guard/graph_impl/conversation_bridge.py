"""ConversationStoreBridge：ConversationBridge 协议的生产适配器（接线片 ②）。

把 :class:`~repopilot_guard.graph_impl.graph_bridge.ConversationBridge` 协议
（load_context / record_request / record_result）对接到真实的
:class:`~repopilot_guard.conversation_store.ConversationStore`：

- ``load_context`` → ``context_for_next_task(conversation_id).model_message()``：
  返回标记为不可信上下文的历史投影字符串；
- ``record_request`` → ``append_task_request``：记录用户本轮输入为
  ``task_request`` 消息；
- ``record_result`` → ``append_task_summary``：写回受控任务结论为
  ``task_summary`` 消息（按 task_thread_id 幂等）。

为什么 ConversationStoreBridge 在构造时绑定 ``conversation_id``——因为
``ConversationBridge`` 协议是「单会话」视角（一位管家对应一个会话），
而 ``ConversationStore`` 的方法都按 ``conversation_id`` 索引。绑定后
桥接器调用方无需每次传 ``conversation_id``，与 DR-024 桥接器语义一致。

与 OrchestratedGraphRunner 的协作（片 ③ 接线时落地）：

```python
orchestrated = OrchestratedGraphRunner(runner, store, conversations, slots, obs)
bridge = ConversationStoreBridge(conversations, conversation_id="c1")
handle = GraphAgentHandle(
    runner=orchestrated,
    request_factory=factory,
    conversation=bridge,
)
```

OrchestratedGraphRunner 的 ``_append_summary`` 已经直接调
``ConversationStore.append_task_summary``（经 api.py 的
``_append_conversation_task_summary`` 包装）——所以**桥接器的 record_result
是冗余路径**，只在接线片 ③ 切换到 GraphAgentHandle 驱动后才会被走到
（那时 OrchestratedGraphRunner 不再直接持有 conversations）。为避免重复
写入，桥接器 ``record_result`` 只在 ``task_summary`` 尚未写入时追加，
依赖 ``append_task_summary`` 的幂等性。

本模块只依赖 ConversationStore，不引入循环；测试用 TemporaryDirectory
+ 真实 ConversationStore 钉死三方法语义。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from repopilot_guard.conversation_store import ConversationStore


# 终态任务状态集合——与 api.py _TERMINAL_TASK_STATUSES 对齐
_TERMINAL_TASK_STATUSES = frozenset(
    {"PASSED", "FAILED", "CANCELLED", "BLOCKED", "UNVERIFIED"}
)


class ConversationStoreBridge:
    """把 ConversationStore 适配为 ConversationBridge 协议。

    每个桥接器实例绑定一个 ``conversation_id``——对应一位管家管一个会话。
    多会话需要多个桥接器实例（由片 ③ 的 HandleRegistry 装配）。
    """

    def __init__(
        self,
        conversations: "ConversationStore",
        *,
        conversation_id: str,
    ) -> None:
        self._conversations = conversations
        self._conversation_id = conversation_id

    @property
    def conversation_id(self) -> str:
        """绑定的会话 ID（供 HandleRegistry 与测试观测）。"""
        return self._conversation_id

    # ------------------------------------------------------------------
    # ConversationBridge 协议
    # ------------------------------------------------------------------

    def load_context(self) -> str:
        """读取下一轮任务可用的会话上下文。

        对齐 ``context_for_next_task(conversation_id).model_message()``——
        返回标记为「不可信上下文」的历史投影字符串。如果会话还没有历史，
        返回空串（GraphAgentHandle._run_round 会跳过空上下文）。
        """
        history = self._conversations.context_for_next_task(self._conversation_id)
        if not (history.summary or history.messages):
            return ""
        return history.model_message()

    def record_request(self, content: str) -> None:
        """记录用户本轮输入为 ``task_request`` 消息。

        对齐 ``append_task_request``——但 ``append_task_request`` 需要
        ``task_thread_id``，且 ``conversation_messages`` 表对
        ``(conversation_id, task_thread_id, kind='task_request')`` 有唯一
        约束（防止同任务重复记录请求）。而桥接器此时可能还没拿到
        thread_id（thread_id 由 RequestFactory 在 _run_round 里 record_request
        之后生成）。

        解决：每次 record_request 生成一个唯一占位 thread_id（UUID），保证
        不违反唯一约束。真实 thread_id 的关联靠后续 ``record_result`` 写
        ``task_summary``（kind 不同，不冲突）——task_summary 的幂等性按
        真实 thread_id 去重。

        占位 thread_id 仅用于绕过唯一约束，不参与任务关联——接线片 ③
        若 RequestFactory 改为先返回 thread_id 再调 record_request，可
        改为传真实 thread_id。
        """
        from uuid import uuid4

        placeholder_thread_id = f"pending-{uuid4().hex[:12]}"
        self._conversations.append_task_request(
            self._conversation_id,
            content=content,
            task_thread_id=placeholder_thread_id,
        )

    def record_result(self, thread_id: str, status: str, result: Any) -> None:
        """写回受控任务结论为 ``task_summary`` 消息。

        对齐 ``append_task_summary``——只取受控状态（status/verdict），
        不读取工具原文或文件内容。依赖 ``append_task_summary`` 的幂等性
        （按 task_thread_id 去重），与 OrchestratedGraphRunner 的
        ``_append_summary`` 重复写入不会产生重复消息。

        非终态状态（如 RUNNING / WAITING_APPROVAL）不写回——避免把
        中间态误投影成会话消息。这与 api.py ``_append_conversation_task_summary``
        的 ``task.status not in _TERMINAL_TASK_STATUSES: return False``
        对齐。
        """
        if status not in _TERMINAL_TASK_STATUSES:
            return
        verdict = _safe_verdict(result)
        content = _format_summary(status, verdict)
        self._conversations.append_task_summary(
            self._conversation_id,
            content=content,
            task_thread_id=thread_id,
            task_status=status,
            task_verdict=verdict,
        )


# ----------------------------------------------------------------------
# 模块级辅助
# ----------------------------------------------------------------------


def _safe_verdict(result: Any) -> str | None:
    """从 GraphRunResult 或 dict 安全取 verdict。"""
    if result is None:
        return None
    if isinstance(result, dict):
        value = result.get("verdict")
        return value if isinstance(value, str) else None
    verdict = getattr(result, "verdict", None)
    return verdict if isinstance(verdict, str) else None


def _format_summary(status: str, verdict: str | None) -> str:
    """格式化任务摘要内容——只取受控状态，不读工具原文。"""
    parts = [f"任务状态：{status}"]
    if verdict:
        parts.append(f"结论：{verdict}")
    return " | ".join(parts)


__all__ = ["ConversationStoreBridge"]
