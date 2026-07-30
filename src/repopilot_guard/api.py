"""仅供本机桌面端调用的 FastAPI 接口。"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from threading import Event, Thread
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, SecretStr

from repopilot_guard import __version__
from repopilot_guard.capabilities import CapabilityDescriptor, CapabilityPolicy
from repopilot_guard.config import AppSettings, ComponentCheck, RuntimeConfigurationError, RuntimeConfigurationManager
from repopilot_guard.conversation_store import ConversationStore
from repopilot_guard.context import ManagedDocumentStore
from repopilot_guard.document_indexing import index_uploaded_document
from repopilot_guard.graph import GraphRunner, research_capability_registry
from repopilot_guard.mcp import McpConfigError, McpConfigLoader, McpConfiguration
from repopilot_guard.mcp_runtime import McpRuntime, McpRuntimeError
from repopilot_guard.models import TaskMode, TaskOperation, TaskRequest, WorkspaceSelection
from repopilot_guard.permissions import FULL_ACCESS_CONFIRMATION, PermissionGrant, PermissionMode
from repopilot_guard.plugins import PluginError, PluginRegistry
from repopilot_guard.project_diagnostics import assess_task_admission, diagnose_project
from repopilot_guard.project_registry import ProjectRegistry
from repopilot_guard.providers import OpenAICompatibleProvider
from repopilot_guard.skills import SkillRegistry
from repopilot_guard.task_export import TaskEvidenceExporter
from repopilot_guard.task_diagnostics import build_task_diagnostic, extract_diagnostic_codes
from repopilot_guard.task_progress import build_task_progress
from repopilot_guard.task_store import StoredTask, StoredTaskEvent, TaskStore


_TERMINAL_TASK_STATUSES = frozenset({"REPORT", "BLOCKED", "FAILED", "CANCELLED"})


class CreateTaskBody(BaseModel):
    project_id: str | None = None
    conversation_id: str | None = Field(default=None, max_length=128)
    repository: str | None = None
    description: str = Field(min_length=1, max_length=12_000)
    task_mode: TaskMode = TaskMode.SAFE_ISOLATED
    operation: TaskOperation = TaskOperation.CHANGE
    confirmation: str | None = None
    thread_id: str | None = None
    output_root: str | None = None
    approved_mcp_tools: list[str] = Field(default_factory=list, max_length=64)
    attached_document_ids: list[str] = Field(default_factory=list, max_length=4)


class ApprovalBody(BaseModel):
    approved: bool | None = None
    decision: str | None = Field(default=None, pattern="^(approve|revise|reject)$")
    comment: str | None = Field(default=None, max_length=2000)

    def resolved_decision(self) -> str:
        return self.decision or ("approve" if self.approved is True else "reject")


class CancellationBody(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class McpProbeBody(BaseModel):
    server: str = Field(min_length=1, max_length=64)
    config_path: str = Field(default=".repopilot/mcp.toml", min_length=1, max_length=260)
    task_mode: TaskMode = TaskMode.SAFE_ISOLATED
    confirmation: str | None = None
    approve_risk: bool = False
    force: bool = False


class McpCallBody(McpProbeBody):
    tool: str = Field(min_length=1, max_length=255)
    arguments: dict[str, object] = Field(default_factory=dict)


class PluginInstallBody(BaseModel):
    source: str = Field(min_length=1, max_length=1024)


class PluginEnabledBody(BaseModel):
    enabled: bool


class DocumentIndexBody(BaseModel):
    file: str = Field(min_length=1, max_length=1024)


class ProjectRenameBody(BaseModel):
    display_name: str = Field(min_length=1, max_length=80)


class ConversationCreateBody(BaseModel):
    project_id: str | None = Field(default=None, max_length=128)
    display_title: str | None = Field(default=None, max_length=80)
    mode: str = Field(default="goal", pattern="^(goal|plan)$")


class ConversationUpdateBody(BaseModel):
    project_id: str | None = Field(default=None, max_length=128)
    display_title: str | None = Field(default=None, max_length=80)
    mode: str | None = Field(default=None, pattern="^(goal|plan)$")


class ConversationChatBody(BaseModel):
    """普通会话只接收自然语言，不能在此入口传入工具、权限或工作区参数。"""

    content: str = Field(min_length=1, max_length=12_000)


class ConversationBranchBody(BaseModel):
    """分支只能选择当前会话中的消息，不能注入外部历史或权限状态。"""

    from_message_id: str | None = Field(default=None, max_length=128)


class TaskRenameBody(BaseModel):
    display_title: str = Field(min_length=1, max_length=80)


class RuntimeConfigurationBody(BaseModel):
    """桌面端可编辑字段。密钥只用于本次写入，不会出现在响应中。"""

    chat_base_url: str | None = Field(default=None, max_length=1024)
    chat_api_key: SecretStr | None = Field(default=None, max_length=1024)
    chat_model: str | None = Field(default=None, max_length=256)
    embedding_base_url: str | None = Field(default=None, max_length=1024)
    embedding_api_key: SecretStr | None = Field(default=None, max_length=1024)
    embedding_model: str | None = Field(default=None, max_length=256)
    embedding_dimensions: int | None = Field(default=None, ge=1, le=65_536)
    qdrant_url: str | None = Field(default=None, max_length=1024)

    def update_values(self) -> dict[str, object]:
        values = self.model_dump(exclude_unset=True)
        mapping = {
            "chat_base_url": "REPOPILOT_CHAT_BASE_URL",
            "chat_api_key": "REPOPILOT_CHAT_API_KEY",
            "chat_model": "REPOPILOT_CHAT_MODEL",
            "embedding_base_url": "REPOPILOT_EMBEDDING_BASE_URL",
            "embedding_api_key": "REPOPILOT_EMBEDDING_API_KEY",
            "embedding_model": "REPOPILOT_EMBEDDING_MODEL",
            "embedding_dimensions": "REPOPILOT_EMBEDDING_DIMENSIONS",
            "qdrant_url": "REPOPILOT_QDRANT_URL",
        }
        return {
            mapping[name]: value.get_secret_value() if isinstance(value, SecretStr) else value
            for name, value in values.items()
        }


class TaskExportBody(BaseModel):
    """导出路径必须由用户显式提供，服务端不猜测或覆盖既有文件。"""

    output: str = Field(min_length=1, max_length=1024)


def _desktop_allowed_origins() -> list[str]:
    """仅允许 Tauri 和开发预览显式声明的 loopback 来源。"""

    origins = [
        "http://127.0.0.1:1420",
        "http://localhost:1420",
        "http://tauri.localhost",
        "https://tauri.localhost",
    ]
    configured = os.environ.get("REPOPILOT_DESKTOP_PREVIEW_ORIGIN")
    if not configured:
        return origins
    try:
        parsed = urlparse(configured)
        port = parsed.port
    except ValueError:
        return origins
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or port is None
        or not 1 <= port <= 65_535
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        return origins
    origin = f"{parsed.scheme}://{parsed.hostname}:{port}"
    if origin not in origins:
        origins.append(origin)
    return origins


def create_app(
    runner: GraphRunner,
    registry: ProjectRegistry,
    default_output_root: Path,
    task_store: TaskStore | None = None,
    mcp_runtime_factory: Callable[[McpConfiguration, Path], McpRuntime] | None = None,
    plugin_registry: PluginRegistry | None = None,
    document_indexer: Callable[[str, Path], dict[str, object]] | None = None,
    runtime_health_checks: Callable[[], tuple[ComponentCheck, ...]] | None = None,
    runtime_configuration_manager: RuntimeConfigurationManager | None = None,
    conversation_reply: Callable[[str, str, str | None], str] | None = None,
    conversation_reply_stream: Callable[[str, str, str | None], Iterator[str]] | None = None,
) -> FastAPI:
    """创建 API；调用者负责复用 SQLite graph runner 与项目注册表。"""

    app = FastAPI(title="RepoPilot Guard", version=__version__)
    store = task_store or TaskStore(registry.database_path)
    conversations = ConversationStore(registry.database_path)
    plugins = plugin_registry or PluginRegistry(registry.database_path)
    app.state.task_store = store
    app.state.conversation_store = conversations
    create_mcp_runtime = mcp_runtime_factory or (lambda configuration, root: McpRuntime(configuration, workspace_root=root))
    index_document = document_indexer or (lambda project_id, source: index_uploaded_document(registry, project_id, source))
    check_runtime = runtime_health_checks or (lambda: ())
    runtime_configuration = runtime_configuration_manager or RuntimeConfigurationManager()
    # 当前 FastAPI/Starlette 版本通过 Router 生命周期关闭 SQLite 连接。
    app.router.on_shutdown.append(store.close)
    app.router.on_shutdown.append(conversations.close)
    if plugin_registry is None:
        app.router.on_shutdown.append(plugins.close)
    # 开发期前端运行在 Vite 的独立本机端口，生产权限仍由 Python 后端裁决。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_desktop_allowed_origins(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type"],
    )

    @app.get("/api/health")
    def health() -> dict[str, object]:
        try:
            checks = check_runtime()
        except Exception:
            checks = (
                ComponentCheck(
                    component="runtime",
                    ready=False,
                    code="RUNTIME_HEALTH_CHECK_FAILED",
                    message="运行依赖健康检查失败，未暴露内部异常。",
                ),
            )
        return {
            "status": "READY",
            "agent_status": "READY" if all(check.ready for check in checks) else "BLOCKED",
            "version": __version__,
            "scope": "127.0.0.1-only",
            # 前端按能力而非包版本启用可选操作，避免开发预览前后端热更新不同步。
            "capabilities": [
                "task_artifacts",
                "task_artifact_versions",
                "task_evidence_export",
                "runtime_configuration",
                "conversation_messages",
            ],
            "dependencies": [check.to_dict() for check in checks],
        }

    @app.get("/api/runtime/configuration")
    def runtime_configuration_snapshot() -> dict[str, object]:
        """仅返回非敏感配置与密钥是否已设置。"""

        return runtime_configuration.snapshot()

    @app.post("/api/runtime/configuration")
    def update_runtime_configuration(body: RuntimeConfigurationBody) -> dict[str, object]:
        """保存到 Tauri 托管的应用数据目录；开发仓库 `.env` 始终拒绝。"""

        try:
            return runtime_configuration.update(body.update_values())
        except RuntimeConfigurationError as error:
            code = str(error)
            status = 409 if code.startswith("CONFIGURATION_WRITE_") or code == "CONFIGURATION_DIRECTORY_UNAVAILABLE" else 400
            raise HTTPException(status, {"code": code, "message": "运行配置未保存。"}) from error

    @app.get("/api/projects")
    def list_projects(include_archived: bool = False) -> dict[str, object]:
        return {"projects": [item.to_dict() for item in registry.list(include_archived=include_archived)]}

    @app.post("/api/projects")
    def add_project(path: str, name: str | None = None) -> dict[str, object]:
        try:
            return {"project": registry.add(Path(path), name).to_dict()}
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    @app.delete("/api/projects/{project_id}")
    def remove_project(project_id: str) -> dict[str, object]:
        if not registry.remove(project_id):
            raise HTTPException(404, "PROJECT_NOT_FOUND")
        return {"status": "READY"}

    @app.patch("/api/projects/{project_id}")
    def rename_project(project_id: str, body: ProjectRenameBody) -> dict[str, object]:
        try:
            return {"project": registry.rename(project_id, body.display_name).to_dict()}
        except ValueError as error:
            raise HTTPException(400 if str(error) == "PROJECT_DISPLAY_NAME_INVALID" else 404, str(error)) from error

    @app.post("/api/projects/{project_id}/archive")
    def archive_project(project_id: str) -> dict[str, object]:
        try:
            return {"project": registry.archive(project_id).to_dict()}
        except ValueError as error:
            raise HTTPException(404, str(error)) from error

    @app.post("/api/projects/{project_id}/restore")
    def restore_project(project_id: str) -> dict[str, object]:
        try:
            return {"project": registry.restore(project_id).to_dict()}
        except ValueError as error:
            raise HTTPException(404, str(error)) from error

    @app.get("/api/conversations")
    def list_conversations(include_archived: bool = False) -> dict[str, object]:
        return {"conversations": [item.to_dict() for item in conversations.list(include_archived=include_archived)]}

    @app.post("/api/conversations")
    def create_conversation(body: ConversationCreateBody) -> dict[str, object]:
        if body.project_id:
            try:
                project = registry.get(body.project_id)
            except ValueError as error:
                raise HTTPException(404, "PROJECT_NOT_FOUND") from error
            if project.archived_at:
                raise HTTPException(409, "PROJECT_ARCHIVED")
        try:
            return {
                "conversation": conversations.create(
                    project_id=body.project_id,
                    display_title=body.display_title,
                    mode=body.mode,
                ).to_dict()
            }
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    @app.patch("/api/conversations/{conversation_id}")
    def update_conversation(conversation_id: str, body: ConversationUpdateBody) -> dict[str, object]:
        project_id: str | None | object = ...
        if "project_id" in body.model_fields_set:
            project_id = body.project_id
            if project_id:
                try:
                    project = registry.get(project_id)
                except ValueError as error:
                    raise HTTPException(404, "PROJECT_NOT_FOUND") from error
                if project.archived_at:
                    raise HTTPException(409, "PROJECT_ARCHIVED")
        try:
            return {
                "conversation": conversations.update(
                    conversation_id,
                    display_title=body.display_title if "display_title" in body.model_fields_set else None,
                    project_id=project_id,
                    mode=body.mode,
                ).to_dict()
            }
        except ValueError as error:
            status = 404 if str(error) == "CONVERSATION_NOT_FOUND" else 400
            raise HTTPException(status, str(error)) from error

    @app.post("/api/conversations/{conversation_id}/archive")
    def archive_conversation(conversation_id: str) -> dict[str, object]:
        try:
            return {"conversation": conversations.archive(conversation_id).to_dict()}
        except ValueError as error:
            raise HTTPException(404, str(error)) from error

    @app.post("/api/conversations/{conversation_id}/restore")
    def restore_conversation(conversation_id: str) -> dict[str, object]:
        try:
            return {"conversation": conversations.restore(conversation_id).to_dict()}
        except ValueError as error:
            raise HTTPException(404, str(error)) from error

    @app.post("/api/conversations/{conversation_id}/branches")
    def branch_conversation(
        conversation_id: str,
        body: ConversationBranchBody,
    ) -> dict[str, object]:
        try:
            branch = conversations.fork(
                conversation_id,
                from_message_id=body.from_message_id,
            )
            return {
                "conversation": branch.to_dict(),
                "context": conversations.context_for_next_task(
                    branch.conversation_id
                ).to_dict(),
            }
        except ValueError as error:
            code = str(error)
            status = 404 if code in {
                "CONVERSATION_NOT_FOUND",
                "CONVERSATION_BRANCH_MESSAGE_NOT_FOUND",
            } else 400
            raise HTTPException(status, code) from error

    @app.get("/api/conversations/{conversation_id}/messages")
    def conversation_messages(conversation_id: str) -> dict[str, object]:
        try:
            _backfill_conversation_task_summaries(
                conversations,
                store,
                runner,
                conversation_id,
            )
            context = conversations.context_for_next_task(conversation_id)
            return {
                "messages": [item.to_dict() for item in conversations.messages(conversation_id)],
                "context": context.to_dict(),
            }
        except ValueError as error:
            raise HTTPException(404, str(error)) from error

    @app.post("/api/conversations/{conversation_id}/chat")
    def chat_in_conversation(conversation_id: str, body: ConversationChatBody) -> dict[str, object]:
        """处理不涉及仓库工具的自然语言对话，不创建 LangGraph 任务。"""

        try:
            conversation = conversations.get(conversation_id)
        except ValueError as error:
            raise HTTPException(404, "CONVERSATION_NOT_FOUND") from error
        if conversation.archived_at:
            raise HTTPException(409, "CONVERSATION_ARCHIVED")
        if any(item.status not in _TERMINAL_TASK_STATUSES for item in store.list_for_conversation(conversation_id)):
            raise HTTPException(409, "CONVERSATION_TASK_RUNNING")

        project_name: str | None = None
        if conversation.project_id:
            try:
                project_name = registry.get(conversation.project_id).display_name
            except ValueError:
                # 项目可能在历史会话仍存在时被归档或移除；普通聊天不读取仓库，不应因此失败。
                project_name = None
        history = conversations.context_for_next_task(conversation_id)
        conversations.append_chat_request(conversation_id, content=body.content)
        history_text = history.model_message() if history.summary or history.messages else ""
        try:
            reply = (conversation_reply or _default_conversation_reply)(
                history_text,
                body.content,
                project_name,
            )
        except Exception:
            reply = "本轮普通对话暂时无法生成回复。代码任务和已保存的会话记录没有受到影响，请稍后重试。"
        response = conversations.append_chat_response(conversation_id, content=reply)
        return {
            "status": "READY",
            "message": response.to_dict(),
            "context": conversations.context_for_next_task(conversation_id).to_dict(),
        }

    @app.post("/api/conversations/{conversation_id}/chat/stream")
    def stream_chat_in_conversation(
        conversation_id: str,
        body: ConversationChatBody,
    ) -> StreamingResponse:
        """以 SSE 输出普通对话增量，完整回复只在正常结束后写入会话历史。"""

        try:
            conversation = conversations.get(conversation_id)
        except ValueError as error:
            raise HTTPException(404, "CONVERSATION_NOT_FOUND") from error
        if conversation.archived_at:
            raise HTTPException(409, "CONVERSATION_ARCHIVED")
        if any(item.status not in _TERMINAL_TASK_STATUSES for item in store.list_for_conversation(conversation_id)):
            raise HTTPException(409, "CONVERSATION_TASK_RUNNING")

        project_name: str | None = None
        if conversation.project_id:
            try:
                project_name = registry.get(conversation.project_id).display_name
            except ValueError:
                project_name = None
        history = conversations.context_for_next_task(conversation_id)
        request_message = conversations.append_chat_request(conversation_id, content=body.content)
        history_text = history.model_message() if history.summary or history.messages else ""

        def stream() -> Iterator[str]:
            parts: list[str] = []
            emitted_length = 0
            try:
                reply_stream = conversation_reply_stream
                if reply_stream is not None:
                    chunks = reply_stream(history_text, body.content, project_name)
                elif conversation_reply is not None:
                    chunks = _chunk_text(conversation_reply(history_text, body.content, project_name))
                else:
                    chunks = _default_conversation_reply_stream(history_text, body.content, project_name)
                yield _named_sse_event("message", {"message": request_message.to_dict()})
                for chunk in chunks:
                    if not isinstance(chunk, str) or not chunk:
                        continue
                    remaining = 8_000 - emitted_length
                    if remaining <= 0:
                        break
                    safe_chunk = chunk[:remaining]
                    parts.append(safe_chunk)
                    emitted_length += len(safe_chunk)
                    yield _named_sse_event("delta", {"content": safe_chunk})
                reply = "".join(parts).strip()
                if not reply:
                    reply = "本轮对话没有生成可展示的文本。请换一种说法后重试。"
                response = conversations.append_chat_response(conversation_id, content=reply)
                yield _named_sse_event(
                    "done",
                    {
                        "status": "READY",
                        "message": response.to_dict(),
                        "context": conversations.context_for_next_task(conversation_id).to_dict(),
                    },
                )
            except Exception:
                # 不把 Provider 的异常或配置内容暴露到桌面端，也不伪造模型已经完成的结果。
                fallback = "本轮回复在生成过程中中断，未完成的内容没有写入会话记录。请检查模型配置后重试。"
                response = conversations.append_chat_response(conversation_id, content=fallback)
                yield _named_sse_event(
                    "error",
                    {"code": "CHAT_STREAM_FAILED", "message": fallback, "message_record": response.to_dict()},
                )

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/projects/{project_id}/diagnostics")
    def project_diagnostics(project_id: str) -> dict[str, object]:
        """只读返回项目是否满足两种任务模式与 Java/Maven Profile 的前置条件。"""

        try:
            return diagnose_project(registry.get(project_id))
        except ValueError as error:
            raise HTTPException(404, {"code": "PROJECT_NOT_FOUND", "message": "项目不存在。"}) from error

    @app.get("/api/projects/{project_id}/capability-directory")
    def project_capability_directory(project_id: str) -> dict[str, object]:
        """返回不含路径和正文的能力元数据与权限策略投影。"""

        try:
            project = registry.get(project_id)
        except ValueError as error:
            raise HTTPException(404, {"code": "PROJECT_NOT_FOUND", "message": "项目不存在。"}) from error
        return _capability_directory(project.root_path, plugins)

    @app.post("/api/projects/{project_id}/documents")
    def index_project_document(project_id: str, body: DocumentIndexBody) -> dict[str, object]:
        """导入用户显式选择的 MD/TXT 到应用状态目录，再写入项目 RAG。"""

        try:
            payload = index_document(project_id, Path(body.file))
        except ValueError as error:
            code = str(error) if str(error).startswith("DOCUMENT_") or str(error) == "UNSUPPORTED_DOCUMENT_TYPE" else "DOCUMENT_INDEX_INPUT_INVALID"
            raise HTTPException(400, {"code": code, "message": "研发文档不可导入或不符合安全限制。"}) from error
        if payload.get("status") != "READY":
            raise HTTPException(409, payload)
        return payload

    @app.get("/api/projects/{project_id}/documents")
    def list_project_documents(project_id: str) -> dict[str, object]:
        """列出已导入的受控文档元数据，绝不返回原始或管理副本路径。"""

        try:
            registry.get(project_id)
        except ValueError as error:
            raise HTTPException(404, {"code": "PROJECT_NOT_FOUND", "message": "项目不存在。"}) from error
        documents = ManagedDocumentStore(registry.database_path).list_documents(project_id=project_id)
        return {"status": "READY", "documents": [document.to_dict() for document in documents]}

    @app.get("/api/plugins")
    def list_plugins() -> dict[str, object]:
        return {"plugins": [item.to_dict() for item in plugins.list()]}

    @app.post("/api/plugins")
    def install_plugin(body: PluginInstallBody) -> dict[str, object]:
        try:
            return {"plugin": plugins.install(Path(body.source)).to_dict()}
        except PluginError as error:
            raise HTTPException(400, {"code": error.code, "message": error.message}) from error

    @app.post("/api/plugins/{plugin_id}/enabled")
    def set_plugin_enabled(plugin_id: str, body: PluginEnabledBody) -> dict[str, object]:
        try:
            plugin = plugins.enable(plugin_id) if body.enabled else plugins.disable(plugin_id)
            return {"plugin": plugin.to_dict()}
        except PluginError as error:
            raise HTTPException(409, {"code": error.code, "message": error.message}) from error

    @app.delete("/api/plugins/{plugin_id}")
    def remove_plugin(plugin_id: str) -> dict[str, object]:
        try:
            plugins.remove(plugin_id)
            return {"status": "READY", "code": "PLUGIN_REMOVED", "directory_deleted": False}
        except PluginError as error:
            raise HTTPException(404, {"code": error.code, "message": error.message}) from error

    @app.get("/api/plugins/audit")
    def plugin_audit(plugin_id: str | None = None, limit: int = Query(default=100, ge=1, le=500)) -> dict[str, object]:
        try:
            return {"events": list(plugins.audit(plugin_id, limit))}
        except PluginError as error:
            raise HTTPException(400, {"code": error.code, "message": error.message}) from error

    @app.post("/api/projects/{project_id}/mcp/probe")
    async def probe_project_mcp(project_id: str, body: McpProbeBody) -> dict[str, object]:
        payload, ready = await _project_mcp_operation(
            registry,
            create_mcp_runtime,
            project_id,
            body,
        )
        if not ready:
            raise HTTPException(409, payload)
        return payload

    @app.post("/api/projects/{project_id}/mcp/call")
    async def call_project_mcp(project_id: str, body: McpCallBody) -> dict[str, object]:
        payload, ready = await _project_mcp_operation(
            registry,
            create_mcp_runtime,
            project_id,
            body,
        )
        if not ready:
            raise HTTPException(409, payload)
        return payload

    @app.post("/api/tasks")
    def create_task(body: CreateTaskBody) -> dict[str, object]:
        if bool(body.project_id) == bool(body.repository):
            raise HTTPException(400, "PROJECT_ID_OR_REPOSITORY_REQUIRED")
        try:
            repository = registry.get(body.project_id).root_path if body.project_id else Path(str(body.repository))
            conversation_context = ""
            if body.conversation_id:
                try:
                    conversation = conversations.get(body.conversation_id)
                except ValueError as error:
                    raise HTTPException(404, "CONVERSATION_NOT_FOUND") from error
                if conversation.archived_at:
                    raise HTTPException(409, "CONVERSATION_ARCHIVED")
                if conversation.project_id != body.project_id:
                    raise HTTPException(409, "CONVERSATION_PROJECT_MISMATCH")
                history = conversations.context_for_next_task(body.conversation_id)
                if history.summary or history.messages:
                    conversation_context = history.model_message()
            grant = _grant_for_mode(body.task_mode, body.confirmation)
            attached_document_ids = tuple(body.attached_document_ids)
            if attached_document_ids:
                try:
                    ManagedDocumentStore(registry.database_path).require_documents(
                        project_id=body.project_id or "",
                        document_ids=attached_document_ids,
                    )
                except ValueError as error:
                    raise HTTPException(
                        409,
                        {
                            "status": "BLOCKED",
                            "code": str(error),
                            "message": "任务附件必须是当前项目中已经受控导入的 MD/TXT 文档。",
                        },
                    ) from error
            admission = assess_task_admission(repository, body.task_mode, body.operation)
            if not admission.ready:
                raise HTTPException(409, admission.to_dict())
            request = TaskRequest(
                repository=repository,
                description=body.description,
                output_root=Path(body.output_root) if body.output_root else default_output_root,
                project_id=body.project_id,
                conversation_id=body.conversation_id,
                conversation_context=conversation_context,
                workspace_selection=WorkspaceSelection(mode=body.task_mode.workspace_mode),
                approved_mcp_tools=tuple(body.approved_mcp_tools),
                attached_document_ids=attached_document_ids,
                operation=body.operation,
            )
            thread_id = body.thread_id or str(uuid4())
            store.create(
                thread_id=thread_id,
                task_id=request.task_id,
                project_id=body.project_id,
                conversation_id=body.conversation_id,
                repository=repository,
                output_root=request.output_root,
                task_mode=body.task_mode.value,
                task_operation=body.operation.value,
                permission_mode=grant.mode.value,
                workspace_mode=request.workspace_selection.mode.value,
                display_title=body.description,
            )
            if body.conversation_id:
                conversations.append_task_request(
                    body.conversation_id,
                    content=body.description,
                    task_thread_id=thread_id,
                )

            def run_in_background() -> None:
                if not _begin_execution(store, thread_id):
                    return
                heartbeat_stop = Event()
                heartbeat = _start_lease_heartbeat(store, thread_id, heartbeat_stop)
                try:
                    result = runner.run(request, thread_id, grant).to_dict()
                    stored = store.sync_graph_result(result, execution_finished=True)
                    if stored.cancellation_requested_at:
                        store.complete_cancellation(thread_id)
                        stored = store.get(thread_id)
                    _append_conversation_task_summary(conversations, stored, result)
                except Exception as error:
                    # 不把异常细节或环境变量返回给桌面端；图自身的 BLOCKED 事件仍在 checkpoint 中。
                    try:
                        if store.get(thread_id).cancellation_requested_at:
                            store.complete_cancellation(thread_id)
                        else:
                            store.mark_runtime_failure(thread_id, f"TASK_RUNTIME_FAILED: {type(error).__name__}")
                        _append_conversation_task_summary(conversations, store.get(thread_id), {})
                    except ValueError:
                        return
                finally:
                    heartbeat_stop.set()
                    heartbeat.join(timeout=1)

            Thread(target=run_in_background, name=f"repopilot-{request.task_id}", daemon=True).start()
            snapshot = _task_snapshot(runner, store, thread_id)
            # 后台图写入首个 checkpoint 前，也要让客户端拿到稳定的任务语义。
            snapshot.setdefault("task_operation", body.operation.value)
            snapshot.setdefault("task_description", body.description)
            return snapshot
        except ValueError as error:
            status = 409 if str(error) == "CONVERSATION_TASK_RUNNING" else 400
            raise HTTPException(status, str(error)) from error

    @app.get("/api/tasks")
    def list_tasks(
        limit: int = Query(default=50, ge=1, le=200),
        include_archived: bool = Query(default=False),
    ) -> dict[str, object]:
        for recovered in store.reap_expired_leases():
            _append_conversation_task_summary(conversations, recovered, {})
        return {"tasks": [item.to_dict() for item in store.list(limit, include_archived=include_archived)]}

    @app.get("/api/tasks/{thread_id}")
    def task(thread_id: str) -> dict[str, object]:
        try:
            return _task_snapshot(runner, store, thread_id)
        except (KeyError, ValueError) as error:
            raise HTTPException(404, "TASK_NOT_FOUND") from error

    @app.post("/api/tasks/{thread_id}/approval")
    def approval(thread_id: str, body: ApprovalBody) -> dict[str, object]:
        try:
            task = store.get(thread_id)
            if not task.pending_approval:
                raise ValueError("NO_PENDING_APPROVAL")
            store.begin_execution(thread_id)

            def resume_in_background() -> None:
                heartbeat_stop = Event()
                heartbeat = _start_lease_heartbeat(store, thread_id, heartbeat_stop)
                try:
                    result = runner.resume(
                        thread_id,
                        body.approved,
                        decision=body.resolved_decision(),
                        comment=body.comment,
                    ).to_dict()
                    stored = store.sync_graph_result(result, execution_finished=True)
                    if stored.cancellation_requested_at:
                        store.complete_cancellation(thread_id)
                        stored = store.get(thread_id)
                    _append_conversation_task_summary(conversations, stored, result)
                except Exception as error:
                    try:
                        if store.get(thread_id).cancellation_requested_at:
                            store.complete_cancellation(thread_id)
                        else:
                            store.mark_runtime_failure(thread_id, f"TASK_RUNTIME_FAILED: {type(error).__name__}")
                        _append_conversation_task_summary(conversations, store.get(thread_id), {})
                    except ValueError:
                        return
                finally:
                    heartbeat_stop.set()
                    heartbeat.join(timeout=1)

            Thread(target=resume_in_background, name=f"repopilot-resume-{thread_id}", daemon=True).start()
            return store.get(thread_id).to_dict()
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/api/tasks/{thread_id}/cancel")
    def cancel_task(thread_id: str, body: CancellationBody) -> dict[str, object]:
        try:
            task = store.request_cancellation(thread_id, body.reason)
            request_cancellation = getattr(runner, "request_cancellation", None)
            if callable(request_cancellation):
                request_cancellation(thread_id, task.cancellation_reason)
            if task.status == "CANCELLED":
                _append_conversation_task_summary(conversations, task, {})
            return task.to_dict()
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.delete("/api/tasks/{thread_id}")
    def archive_task(thread_id: str) -> dict[str, object]:
        try:
            return {"task": store.archive(thread_id).to_dict()}
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.patch("/api/tasks/{thread_id}")
    def rename_task(thread_id: str, body: TaskRenameBody) -> dict[str, object]:
        try:
            return {"task": store.rename(thread_id, body.display_title).to_dict()}
        except ValueError as error:
            status = 404 if str(error) == "TASK_NOT_FOUND" else 400
            raise HTTPException(status, str(error)) from error

    @app.get("/api/tasks/{thread_id}/events")
    def events(
        thread_id: str,
        after_sequence: int = Query(default=0, ge=0),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        cursor = _event_cursor(after_sequence, last_event_id)

        def stream() -> Iterator[str]:
            emitted = cursor
            for _ in range(240):
                try:
                    snapshot = _task_snapshot(runner, store, thread_id)
                except (KeyError, ValueError):
                    yield "event: error\ndata: {\"code\":\"TASK_NOT_FOUND\"}\n\n"
                    return
                for event in store.events_after(thread_id, emitted):
                    yield _sse_event(event)
                    emitted = event.sequence
                if snapshot.get("status") in {"REPORT", "BLOCKED", "FAILED", "CANCELLED"}:
                    return
                time.sleep(0.5)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/api/tasks/{thread_id}/diff")
    def diff(thread_id: str) -> dict[str, object]:
        try:
            state = runner.get(thread_id).state
        except (KeyError, ValueError) as error:
            raise HTTPException(404, "TASK_NOT_FOUND") from error
        return {"diff": state.get("git_diff") or "", "status": state.get("status")}

    @app.get("/api/tasks/{thread_id}/telemetry")
    def telemetry(thread_id: str) -> dict[str, object]:
        """返回任务遥测汇总；详细内容仍通过受控产物读取。"""

        try:
            return store.telemetry(thread_id)
        except (KeyError, ValueError) as error:
            raise HTTPException(404, "TASK_NOT_FOUND") from error

    @app.get("/api/tasks/{thread_id}/context")
    def task_context(thread_id: str) -> dict[str, object]:
        """返回脱敏上下文快照，供桌面端审阅模型本次可见的来源边界。"""
        try:
            result = runner.get(thread_id)
        except (KeyError, ValueError) as error:
            raise HTTPException(404, "TASK_NOT_FOUND") from error
        snapshot = result.state.get("context_snapshot")
        available = isinstance(snapshot, dict)
        return {
            "thread_id": result.thread_id,
            "status": result.status,
            "available": available,
            "context_snapshot": snapshot if available else None,
            "references": (result.state.get("context_references") or []) if available else [],
            "attached_documents": _safe_attached_documents(result.state.get("attached_documents")),
        }

    @app.get("/api/tasks/{thread_id}/artifacts")
    def list_task_artifacts(thread_id: str) -> dict[str, object]:
        try:
            _task_snapshot(runner, store, thread_id)
            return {"artifacts": [item.to_dict() for item in store.artifacts(thread_id)]}
        except (KeyError, ValueError) as error:
            raise HTTPException(404, "TASK_ARTIFACTS_NOT_FOUND") from error

    @app.post("/api/tasks/{thread_id}/export")
    def export_task_evidence(thread_id: str, body: TaskExportBody) -> dict[str, object]:
        """导出终态任务的审计包，完整性检查始终在服务端执行。"""

        output = Path(body.output).expanduser()
        if not output.is_absolute():
            raise HTTPException(422, "TASK_EXPORT_OUTPUT_MUST_BE_ABSOLUTE")
        try:
            exported = TaskEvidenceExporter(store).export(thread_id, output)
            return {"export": exported.to_dict()}
        except ValueError as error:
            _raise_task_export_error(str(error))

    @app.get("/api/tasks/{thread_id}/artifacts/{kind}/versions")
    def list_task_artifact_versions(thread_id: str, kind: str) -> dict[str, object]:
        try:
            _task_snapshot(runner, store, thread_id)
            return {"versions": [item.to_dict() for item in store.artifact_versions(thread_id, kind)]}
        except (KeyError, ValueError) as error:
            raise HTTPException(404, "TASK_ARTIFACT_VERSIONS_NOT_FOUND") from error

    @app.get("/api/tasks/{thread_id}/artifacts/{kind}/versions/{version}")
    def task_artifact_version(thread_id: str, kind: str, version: int) -> dict[str, object]:
        try:
            _task_snapshot(runner, store, thread_id)
            artifact, content = store.read_artifact_version(thread_id, kind, version)
            return {"artifact": artifact.to_dict(), "content": content}
        except ValueError as error:
            code = str(error)
            if code == "TASK_ARTIFACT_TOO_LARGE":
                raise HTTPException(413, code) from error
            if code == "TASK_ARTIFACT_INTEGRITY_MISMATCH":
                raise HTTPException(409, code) from error
            raise HTTPException(404, code) from error

    @app.get("/api/tasks/{thread_id}/artifacts/{kind}")
    def task_artifact(thread_id: str, kind: str) -> dict[str, object]:
        try:
            _task_snapshot(runner, store, thread_id)
            artifact, content = store.read_artifact(thread_id, kind)
            return {"artifact": artifact.to_dict(), "content": content}
        except ValueError as error:
            code = str(error)
            if code == "TASK_ARTIFACT_TOO_LARGE":
                raise HTTPException(413, code) from error
            if code == "TASK_ARTIFACT_INTEGRITY_MISMATCH":
                raise HTTPException(409, code) from error
            raise HTTPException(404, code) from error

    @app.get("/api/tasks/{thread_id}/report")
    def report(thread_id: str) -> dict[str, object]:
        try:
            result = runner.get(thread_id)
        except (KeyError, ValueError) as error:
            raise HTTPException(404, "TASK_NOT_FOUND") from error
        return {"verdict": result.verdict, "state": result.status, "plan": result.state.get("plan"), "verification": result.state.get("verification_result"), "error": result.state.get("error_summary")}

    return app


async def _project_mcp_operation(
    registry: ProjectRegistry,
    runtime_factory: Callable[[McpConfiguration, Path], McpRuntime],
    project_id: str,
    body: McpProbeBody,
) -> tuple[dict[str, object], bool]:
    """执行一次显式 MCP 管理操作；请求结束后关闭所有连接。"""

    runtime: McpRuntime | None = None
    try:
        project = registry.get(project_id)
        config_path = _project_relative_path(project.root_path, body.config_path)
        configuration = McpConfigLoader.load(config_path)
        permission = _grant_for_mode(body.task_mode, body.confirmation)
        runtime = runtime_factory(configuration, project.root_path)
        connected = await runtime.connect(
            body.server,
            permission,
            approved=body.approve_risk,
            force=body.force,
        )
        if connected.status != "READY":
            await runtime.close()
            return (
                {
                    "status": "BLOCKED",
                    "code": connected.code,
                    "connection": connected.to_dict(),
                    "events": [event.to_dict() for event in runtime.events],
                },
                False,
            )
        if isinstance(body, McpCallBody):
            operation = (
                await runtime.call_tool(
                    body.tool,
                    body.arguments,
                    permission,
                    approved=body.approve_risk,
                )
            ).to_dict()
        else:
            operation = await runtime.ping(body.server)
        closed = await runtime.disconnect(body.server)
        payload = {
            "status": operation.get("status", "BLOCKED"),
            "code": operation.get("code", "MCP_OPERATION_FAILED"),
            "connection": connected.to_dict(),
            "operation": operation,
            "closed": closed.to_dict(),
            "events": [event.to_dict() for event in runtime.events],
        }
        return payload, payload["status"] == "READY"
    except (McpConfigError, McpRuntimeError, ValueError) as error:
        code = getattr(error, "code", str(error))
        if not isinstance(code, str) or not code or len(code) > 80:
            code = "MCP_CONFIGURATION_INVALID"
        return {"status": "BLOCKED", "code": code, "message": "项目 MCP 配置或请求无效。"}, False
    finally:
        if runtime is not None:
            await runtime.close()


def _project_relative_path(project_root: Path, requested: str) -> Path:
    relative = Path(requested)
    if relative.is_absolute():
        raise ValueError("MCP_CONFIG_PATH_MUST_BE_RELATIVE")
    root = project_root.expanduser().resolve()
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError as error:
        raise ValueError("MCP_CONFIG_PATH_ESCAPE") from error
    return target


def _raise_task_export_error(code: str) -> None:
    """把可预期的导出失败转换成稳定、无路径泄露的 HTTP 语义。"""

    if code == "TASK_NOT_FOUND":
        raise HTTPException(404, code)
    if code in {
        "TASK_EXPORT_NOT_FINALIZED",
        "TASK_EXPORT_OUTPUT_EXISTS",
        "TASK_EXPORT_ARTIFACT_INVALID",
        "TASK_ARTIFACT_INTEGRITY_MISMATCH",
    }:
        raise HTTPException(409, code)
    if code in {
        "TASK_EXPORT_ARTIFACT_TOO_LARGE",
        "TASK_EXPORT_EVIDENCE_TOO_LARGE",
        "TASK_EXPORT_TOO_LARGE",
    }:
        raise HTTPException(413, code)
    raise HTTPException(422, code if code.startswith("TASK_EXPORT_") else "TASK_EXPORT_FAILED")


def _safe_attached_documents(value: object) -> list[dict[str, str]]:
    """仅投影用于审阅的任务附件元数据，拒绝 checkpoint 中的路径或内容字段。"""

    if not isinstance(value, list):
        return []
    safe: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        document_id = item.get("document_id")
        display_name = item.get("display_name")
        content_sha256 = item.get("content_sha256")
        if not (
            isinstance(document_id, str)
            and len(document_id) == 64
            and all(character in "0123456789abcdef" for character in document_id)
            and isinstance(display_name, str)
            and 0 < len(display_name) <= 128
            and all(character.isascii() and (character.isalnum() or character in "._-") for character in display_name)
            and isinstance(content_sha256, str)
            and len(content_sha256) == 64
            and all(character in "0123456789abcdef" for character in content_sha256)
        ):
            continue
        safe.append(
            {
                "document_id": document_id,
                "display_name": display_name,
                "content_sha256": content_sha256,
            }
        )
    return safe[:4]


def _task_snapshot(
    runner: GraphRunner,
    task_store: TaskStore,
    thread_id: str,
) -> dict[str, object]:
    """优先读取 checkpoint 并同步索引；首个 checkpoint 前返回持久化任务记录。"""

    task_store.reap_expired_leases()
    persisted = task_store.get(thread_id)
    runtime_failed = (persisted.error_summary or "").startswith("TASK_RUNTIME_FAILED")
    if persisted.lease_expires_at or persisted.cancellation_requested_at or persisted.error_summary == "TASK_LEASE_EXPIRED":
        return _with_task_progress(persisted.to_dict(), task_store)
    if runtime_failed:
        try:
            result = runner.get(thread_id).to_dict()
            persisted = task_store.sync_graph_result(result, execution_finished=False)
            snapshot = persisted.to_dict()
            state = result.get("state")
            if not isinstance(state, dict):
                state = {}
            snapshot["task_operation"] = state.get("task_operation", persisted.task_operation)
            snapshot["task_description"] = state.get("task_description", persisted.display_title or "")
        except (KeyError, ValueError, sqlite3.Error):
            snapshot = persisted.to_dict()
            snapshot["task_operation"] = persisted.task_operation
            snapshot["task_description"] = persisted.display_title or ""
        return _with_task_progress(snapshot, task_store)
    try:
        result = runner.get(thread_id).to_dict()
        indexed = task_store.sync_graph_result(result, execution_finished=False)
        state = result.get("state")
        if isinstance(state, dict):
            result["task_operation"] = state.get("task_operation", TaskOperation.CHANGE.value)
            result["task_description"] = state.get("task_description", indexed.display_title or "")
        result.update(
            {
                "trace_id": indexed.trace_id,
                "task_id": indexed.task_id,
                "display_title": indexed.display_title,
                "project_id": indexed.project_id,
                "conversation_id": indexed.conversation_id,
                "task_mode": indexed.task_mode,
                "created_at": indexed.created_at,
                "updated_at": indexed.updated_at,
                "archived_at": indexed.archived_at,
            }
        )
        return _with_task_progress(result, task_store)
    except (KeyError, ValueError):
        return _with_task_progress(task_store.get(thread_id).to_dict(), task_store)


def _with_task_progress(snapshot: dict[str, object], task_store: TaskStore) -> dict[str, object]:
    """仅以状态、节点名称和验证结论生成进度，不把事件正文暴露给任务摘要。"""

    state = snapshot.get("state")
    graph_state = state if isinstance(state, dict) else {}
    raw_events = graph_state.get("tool_events")
    persisted_events: list[StoredTaskEvent] = []
    try:
        persisted_events = task_store.events_after(str(snapshot.get("thread_id", "")), 0)
    except (ValueError, sqlite3.Error):
        persisted_events = []
    if not isinstance(raw_events, list):
        raw_events = [event.payload for event in persisted_events]
    enriched = dict(snapshot)
    error_summary = snapshot.get("error_summary")
    if not isinstance(error_summary, str):
        error_summary = graph_state.get("error_summary")
    enriched["progress"] = build_task_progress(
        status=snapshot.get("status"),
        pending_approval=snapshot.get("pending_approval", False),
        pending_approval_action=graph_state.get("pending_approval_action"),
        verdict=snapshot.get("verdict"),
        task_operation=graph_state.get("task_operation", snapshot.get("task_operation", TaskOperation.CHANGE.value)),
        tool_events=raw_events,
        verification=graph_state.get("verification_result"),
    )
    enriched["diagnostic"] = build_task_diagnostic(
        status=snapshot.get("status"),
        verdict=snapshot.get("verdict"),
        pending_approval=snapshot.get("pending_approval", False),
        error_summary=error_summary,
        evidence_codes=(
            extract_diagnostic_codes(raw_events)
            | extract_diagnostic_codes(persisted_events)
        ),
    )
    if isinstance(snapshot.get("state"), dict):
        enriched["state"] = _safe_task_state(graph_state)
    return enriched


def _safe_task_state(state: dict[str, object]) -> dict[str, object]:
    """任务详情只提供桌面端需要的状态，不回传 Graph checkpoint 的内部字段。"""

    safe: dict[str, object] = {}
    for key in ("task_operation", "task_description", "pending_approval_action"):
        value = state.get(key)
        if isinstance(value, str) and value:
            safe[key] = value
    plan = _safe_task_plan(state.get("plan"))
    if plan is not None:
        safe["plan"] = plan
    return safe


def _safe_task_plan(value: object) -> dict[str, object] | None:
    """保留审批 UI 所需的计划字段，拒绝任意嵌套对象和模型附带数据。"""

    if not isinstance(value, dict):
        return None
    safe: dict[str, object] = {}
    for key in ("summary", "verification_recipe", "target_test_class"):
        item = value.get(key)
        if isinstance(item, str) and item:
            safe[key] = item[:4_000]
    for key in ("candidate_files", "steps", "verification", "assumptions", "risks"):
        item = value.get(key)
        if not isinstance(item, list):
            continue
        safe[key] = [entry[:2_000] for entry in item if isinstance(entry, str)][:100]
    return safe


def _backfill_conversation_task_summaries(
    conversations: ConversationStore,
    store: TaskStore,
    runner: GraphRunner,
    conversation_id: str,
) -> None:
    """服务重启或终态竞态后，按持久任务补齐尚未写入的助手总结。"""

    for task in store.list_for_conversation(conversation_id):
        if task.status not in _TERMINAL_TASK_STATUSES:
            continue
        graph_result: dict[str, object] = {}
        try:
            checkpoint = runner.get(task.thread_id)
        except (KeyError, ValueError, sqlite3.Error):
            checkpoint = None
        if checkpoint is not None:
            to_dict = getattr(checkpoint, "to_dict", None)
            if callable(to_dict):
                candidate = to_dict()
                if isinstance(candidate, dict):
                    graph_result = candidate
        _append_conversation_task_summary(conversations, task, graph_result)


def _append_conversation_task_summary(
    conversations: ConversationStore,
    task: StoredTask,
    graph_result: dict[str, object],
) -> bool:
    """将终态任务写入会话；总结只取受控状态，不读取工具原文或文件内容。"""

    if not task.conversation_id or task.status not in _TERMINAL_TASK_STATUSES:
        return False
    state = graph_result.get("state")
    safe_state = state if isinstance(state, dict) else {}
    try:
        conversations.append_task_summary(
            task.conversation_id,
            content=_conversation_task_summary(task, safe_state),
            task_thread_id=task.thread_id,
            task_status=task.status,
            task_verdict=task.verdict,
        )
    except (ValueError, sqlite3.Error):
        # 会话投影故障不能篡改已经由 Diff/Maven 证据确定的任务结论；读取接口会再次补齐。
        return False
    return True


def _conversation_task_summary(task: StoredTask, state: dict[str, object]) -> str:
    """生成面向用户的稳定结论，不将模型计划等同于修复成功。"""

    outcome = {
        "PASSED": "任务已完成：已记录真实代码修改且 Maven 验证通过。",
        "UNVERIFIED": "已完成分析，但尚未获得足够的修复与验证证据。",
        "FAILED": "任务尚未完成：补丁、测试或行为验证失败。",
        "BLOCKED": "任务暂时无法继续：权限、审批、配置或运行环境阻断了执行。",
        "CANCELLED": "任务已停止，未继续执行后续修改或验证。",
    }.get(task.verdict or "", "任务已结束，尚未形成可验证的成功结论。")
    lines = ["处理总结", "", f"结论：{outcome}"]
    plan = state.get("plan")
    if isinstance(plan, dict) and isinstance(plan.get("summary"), str):
        lines.extend(("", "分析摘要：", str(plan["summary"])[:4_000]))
    if state.get("git_diff"):
        lines.extend(("", "修改证据：已生成真实 Git Diff。"))
    verification = state.get("verification_result")
    if isinstance(verification, dict):
        status = verification.get("status")
        recipe = verification.get("recipe")
        detail = f"验证结果：{status or 'UNKNOWN'}"
        if isinstance(recipe, str) and recipe:
            detail += f"（{recipe}）"
        lines.extend(("", detail + "。"))
    elif task.verdict != "PASSED":
        lines.extend(("", "验证结果：本轮没有可证明成功的 Maven 验证记录。"))
    if task.verdict == "UNVERIFIED":
        lines.extend(("", "下一步：可以继续同一对话补充目标，或切换到目标模式执行受控修复。"))
    elif task.verdict in {"BLOCKED", "FAILED"}:
        lines.extend(("", "下一步：查看详情中的诊断与证据后，补充条件或调整目标再继续。"))
    return "\n".join(lines)


def _default_conversation_reply(history: str, content: str, project_name: str | None) -> str:
    """调用普通对话模型；不可用时返回明确边界，绝不伪造代码检索或执行结果。"""

    try:
        return "".join(_default_conversation_reply_stream(history, content, project_name)).strip()
    except Exception:
        return "普通对话模型暂时不可用。你可以稍后重试；RepoPilot 没有执行任何代码操作。"


def _default_conversation_reply_stream(
    history: str,
    content: str,
    project_name: str | None,
) -> Iterator[str]:
    """输出 OpenAI-compatible 模型文本分片；普通对话不注册仓库或系统工具。"""

    try:
        provider = OpenAICompatibleProvider(AppSettings())
        readiness = provider.chat_check()
        if not readiness.ready:
            yield "对话模型尚未配置。你仍可以选择项目并发起受控代码分析或修复任务；配置完成后，我可以处理普通对话。"
            return
        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    "你是 RepoPilot 的本地 Coding Assistant，正在普通对话模式。"
                    "请使用中文、简洁自然地回答。当前模式不读取仓库、不调用工具、不创建任务，"
                    "不能声称已经检查代码、执行命令或完成修复。"
                    "当用户明确希望分析项目、定位代码或修改代码时，提示可以切换到对应代码任务模式。"
                ),
            }
        ]
        if project_name:
            messages.append({"role": "system", "content": f"当前关联项目名称：{project_name}。"})
        if history:
            messages.append({"role": "user", "content": history})
        messages.append({"role": "user", "content": content})
        emitted = False
        for message_chunk in provider.create_chat_model().stream(messages):
            chunk = _chat_chunk_text(message_chunk.content)
            if chunk:
                emitted = True
                yield chunk
        if not emitted:
            yield "我收到了这条消息，但模型没有返回可展示的文本。你可以换一种说法，或发起代码分析任务。"
    except Exception:
        raise


def _chat_chunk_text(raw_content: object) -> str:
    """兼容 LangChain 在少数兼容模型中返回的多段 content 格式。"""

    if isinstance(raw_content, str):
        return raw_content
    if not isinstance(raw_content, list):
        return ""
    return "".join(
        item.get("text", "")
        for item in raw_content
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    )


def _chunk_text(content: str, *, size: int = 96) -> Iterator[str]:
    """使测试替身和非流式注入回调也遵循稳定的增量事件协议。"""

    for index in range(0, len(content), size):
        yield content[index : index + size]


def _named_sse_event(event: str, payload: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _event_cursor(after_sequence: int, last_event_id: str | None) -> int:
    if not last_event_id:
        return after_sequence
    try:
        return max(after_sequence, int(last_event_id.rsplit(":", 1)[1]))
    except (IndexError, ValueError):
        return after_sequence


def _sse_event(event: StoredTaskEvent) -> str:
    event_name = "state" if event.event_type == "TASK_STATE" else "evidence"
    public_event = event.to_public_dict()
    public_payload = public_event["payload"]
    assert isinstance(public_payload, dict)
    payload = {
        "sequence": event.sequence,
        "event_id": event.event_id,
        "trace_id": event.trace_id,
        "type": event.event_type,
        **public_payload,
    }
    return f"id: {event.event_id}\nevent: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _begin_execution(store: TaskStore, thread_id: str) -> bool:
    try:
        store.begin_execution(thread_id)
        return True
    except ValueError:
        # 取消可能恰好发生在后台线程真正启动之前；此时不再触发模型或工具。
        return False


def _start_lease_heartbeat(store: TaskStore, thread_id: str, stop: Event) -> Thread:
    def renew() -> None:
        # 最大 30 秒间隔，默认 15 分钟租约下可容忍 API 进程短暂阻塞。
        while not stop.wait(30):
            try:
                store.renew_lease(thread_id)
            except ValueError:
                return

    worker = Thread(target=renew, name=f"repopilot-lease-{thread_id}", daemon=True)
    worker.start()
    return worker


def _capability_directory(project_root: Path, plugins: PluginRegistry) -> dict[str, object]:
    """构造项目级只读能力目录，绝不将发现路径或 Skill 正文返回给桌面端。"""

    policy = CapabilityPolicy()
    safe_grant = PermissionGrant.safe()
    full_grant = PermissionGrant(PermissionMode.FULL, FULL_ACCESS_CONFIRMATION)
    skill_registry = SkillRegistry.discover(
        project_root=project_root,
        plugin_roots=plugins.active_skill_roots(),
    )
    descriptors = [*research_capability_registry().list(), *skill_registry.capabilities()]
    capabilities = [
        _public_capability_descriptor(descriptor, policy, safe_grant, full_grant)
        for descriptor in sorted(descriptors, key=lambda item: item.capability_id)
    ]
    plugin_items = [
        {
            "plugin_id": plugin.plugin_id,
            "name": plugin.manifest.name,
            "version": plugin.manifest.version,
            "description": plugin.manifest.description,
            "enabled": plugin.enabled,
            "integrity_status": plugin.integrity_status,
            "active": plugin.enabled and plugin.integrity_status == "VERIFIED",
        }
        for plugin in plugins.list()
    ]
    return {
        "status": "READY",
        "capabilities": capabilities,
        "plugins": plugin_items,
        "issues": [{"code": issue.code, "message": issue.message} for issue in skill_registry.issues],
    }


def _public_capability_descriptor(
    descriptor: CapabilityDescriptor,
    policy: CapabilityPolicy,
    safe_grant: PermissionGrant,
    full_grant: PermissionGrant,
) -> dict[str, object]:
    """将能力描述投影为 UI 所需字段，不透传来源路径或扩展配置。"""

    metadata = dict(descriptor.metadata)
    details = {
        key: metadata[key]
        for key in ("allowed_tools", "user_invocable", "disable_model_invocation", "content_sha256")
        if key in metadata
    }
    scope_labels = {
        "bundled": "RepoPilot 内置",
        "plugin": "已验证插件",
        "user": "本机用户",
        "project": "当前项目",
    }
    return {
        "capability_id": descriptor.capability_id,
        "name": descriptor.name,
        "description": descriptor.description,
        "kind": descriptor.kind.value,
        "scope": descriptor.scope.value,
        "source_label": scope_labels[descriptor.scope.value],
        "risks": sorted(risk.value for risk in descriptor.risks),
        "enabled": descriptor.enabled,
        "details": details,
        "safe_policy": policy.decide(descriptor, safe_grant).to_dict(),
        "full_policy": policy.decide(descriptor, full_grant).to_dict(),
    }


def _grant_for_mode(mode: TaskMode, confirmation: str | None) -> PermissionGrant:
    if mode is TaskMode.SAFE_ISOLATED:
        return PermissionGrant.safe()
    if confirmation != FULL_ACCESS_CONFIRMATION:
        raise ValueError("FULL_ACCESS_CONFIRMATION_REQUIRED")
    return PermissionGrant(PermissionMode.FULL, confirmation)
