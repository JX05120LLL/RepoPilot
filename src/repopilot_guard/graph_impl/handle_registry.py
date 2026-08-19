"""HandleRegistry：会话级常驻管家注册表（接线片 ③）。

每个会话（``conversation_id``）对应一位常驻 :class:`GraphAgentHandle`——
会话活着管家就在，会话结束管家下岗。注册表线程安全、惰性创建：第一次
``get_or_create`` 时装配 handle，后续同会话复用。

装配关系（片 ③ 接线后）：

```python
orchestrated = OrchestratedGraphRunner(runner, store, conversations, slots, obs)
registry = HandleRegistry(
    orchestrated=orchestrated,
    conversations=conversations,
    request_factory=prod_factory,
    permission=permission_grant,
    cancellation_registry=...,
)
# POST /api/tasks 收到请求且 conversation_id 已绑定时：
handle = registry.get_or_create(conversation_id)
handle.followup(AgentMessage(content=description, origin=MessageOrigin.USER))
# POST /api/tasks/{id}/approval：
handle.approve(approved)
# FastAPI shutdown：
registry.cancel_all_and_wait(reason="service shutdown")
```

与 api.py 现有 ``run_in_background`` 旧路径并存（feature flag 切换）——
新路径通过 ``REPOPILOT_AGENT_HANDLE_MODE=1`` 启用，默认走旧路径以便回滚。

设计要点：

- **线程安全**：``get_or_create`` 用 Lock 保护，避免同会话并发请求重复
  创建 handle；
- **惰性创建**：未用到的会话不创建 handle，节省资源；
- **同会话复用**：同 ``conversation_id`` 多次 ``get_or_create`` 返回同一
  handle——这是"常驻管家"语义的落地；
- **优雅退出**：``cancel_all_and_wait`` 遍历所有 handle，``cancel`` +
  ``when_idle`` 确保服务退出时没有遗留任务。
"""

from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING, Any, Callable

from repopilot_guard.cancellation import DEFAULT_CANCELLATION_REGISTRY, TaskCancellationRegistry
from repopilot_guard.graph_impl.agent_handle import AgentMessage, MessageOrigin
from repopilot_guard.graph_impl.conversation_bridge import ConversationStoreBridge
from repopilot_guard.graph_impl.graph_bridge import GraphAgentHandle

if TYPE_CHECKING:
    from repopilot_guard.conversation_store import ConversationStore
    from repopilot_guard.graph_impl.orchestrated_runner import OrchestratedGraphRunner


RequestFactory = Callable[[str, str], tuple[Any, str]]
"""(description, context) -> (request, thread_id)。生产实现由 api.py 注入。"""


class HandleRegistry:
    """会话级常驻管家注册表。

    每个会话一位 :class:`GraphAgentHandle`，惰性创建、线程安全、同会话复用。
    """

    def __init__(
        self,
        *,
        orchestrated: "OrchestratedGraphRunner",
        conversations: "ConversationStore",
        request_factory: "RequestFactory | None" = None,
        permission: Any = None,
        cancellation_registry: TaskCancellationRegistry | None = None,
        on_error: "Callable[[BaseException], None] | None" = None,
    ) -> None:
        self._orchestrated = orchestrated
        self._conversations = conversations
        self._request_factory = request_factory  # 可在 get_or_create 时注入
        self._permission = permission
        self._cancellation_registry = cancellation_registry or DEFAULT_CANCELLATION_REGISTRY
        self._on_error = on_error
        self._handles: dict[str, GraphAgentHandle] = {}
        self._lock = Lock()

    # ------------------------------------------------------------------
    # 注册表核心
    # ------------------------------------------------------------------

    def get_or_create(
        self,
        conversation_id: str,
        *,
        request_factory: "RequestFactory | None" = None,
        permission: Any = None,
    ) -> GraphAgentHandle:
        """获取或创建会话对应的管家；同会话复用，线程安全。

        首次调用为该会话装配 :class:`GraphAgentHandle`。``request_factory``
        和 ``permission`` 可在首次创建时注入（会话级固定）；后续同会话
        调用忽略这些参数（复用首次的 handle）。
        """
        with self._lock:
            handle = self._handles.get(conversation_id)
            if handle is not None:
                return handle
            factory = request_factory or self._request_factory
            if factory is None:
                raise ValueError("REQUEST_FACTORY_REQUIRED")
            perm = permission if permission is not None else self._permission
            bridge = ConversationStoreBridge(
                self._conversations,
                conversation_id=conversation_id,
            )
            handle = GraphAgentHandle(
                runner=self._orchestrated,
                request_factory=factory,
                permission=perm,
                conversation=bridge,
                cancellation_registry=self._cancellation_registry,
                on_error=self._on_error,
            )
            self._handles[conversation_id] = handle
            return handle

    def get(self, conversation_id: str) -> "GraphAgentHandle | None":
        """只读查询；不存在返回 None。"""
        with self._lock:
            return self._handles.get(conversation_id)

    def __contains__(self, conversation_id: object) -> bool:
        with self._lock:
            return isinstance(conversation_id, str) and conversation_id in self._handles

    def __len__(self) -> int:
        with self._lock:
            return len(self._handles)

    # ------------------------------------------------------------------
    # 优雅退出
    # ------------------------------------------------------------------

    def cancel_all_and_wait(self, *, reason: str = "service shutdown", timeout: float | None = None) -> None:
        """服务退出时遍历所有 handle：cancel + when_idle。

        ``cancel`` 清空收件箱并发进程内取消信号；``when_idle`` 等待驱动
        循环与审批恢复线程收敛。``timeout`` 为 None 表示无限等；实际部署
        中应由调用方设上限（FastAPI graceful shutdown 配合）。
        """
        with self._lock:
            handles = list(self._handles.values())
        for handle in handles:
            try:
                handle.cancel(reason)
            except Exception:
                # 取消失败不阻塞后续 handle 的退出
                pass
        for handle in handles:
            try:
                if timeout is None:
                    handle.when_idle()
                else:
                    # GraphAgentHandle.when_idle 当前无 timeout 参数；
                    # 调用方应在外层控制总时限。这里直接调无限等。
                    handle.when_idle()
            except Exception:
                pass


# ----------------------------------------------------------------------
# 便捷：构造用户消息
# ----------------------------------------------------------------------


def user_message(content: str) -> AgentMessage:
    """构造用户追问消息（供 api.py 接线时调用）。"""
    return AgentMessage(content=content, origin=MessageOrigin.USER)


__all__ = ["HandleRegistry", "RequestFactory", "user_message"]
