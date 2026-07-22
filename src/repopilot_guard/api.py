"""仅供本机桌面端调用的 FastAPI 接口。"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from repopilot_guard import __version__
from repopilot_guard.config import ComponentCheck
from repopilot_guard.context import ManagedDocumentStore
from repopilot_guard.document_indexing import index_uploaded_document
from repopilot_guard.graph import GraphRunner
from repopilot_guard.mcp import McpConfigError, McpConfigLoader, McpConfiguration
from repopilot_guard.mcp_runtime import McpRuntime, McpRuntimeError
from repopilot_guard.models import TaskMode, TaskRequest, WorkspaceSelection
from repopilot_guard.permissions import FULL_ACCESS_CONFIRMATION, PermissionGrant, PermissionMode
from repopilot_guard.plugins import PluginError, PluginRegistry
from repopilot_guard.project_diagnostics import diagnose_project
from repopilot_guard.project_registry import ProjectRegistry
from repopilot_guard.task_store import StoredTaskEvent, TaskStore


class CreateTaskBody(BaseModel):
    project_id: str | None = None
    repository: str | None = None
    description: str = Field(min_length=1)
    task_mode: TaskMode = TaskMode.SAFE_ISOLATED
    confirmation: str | None = None
    thread_id: str | None = None
    output_root: str | None = None
    approved_mcp_tools: list[str] = Field(default_factory=list, max_length=64)


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


def create_app(
    runner: GraphRunner,
    registry: ProjectRegistry,
    default_output_root: Path,
    task_store: TaskStore | None = None,
    mcp_runtime_factory: Callable[[McpConfiguration, Path], McpRuntime] | None = None,
    plugin_registry: PluginRegistry | None = None,
    document_indexer: Callable[[str, Path], dict[str, object]] | None = None,
    runtime_health_checks: Callable[[], tuple[ComponentCheck, ...]] | None = None,
) -> FastAPI:
    """创建 API；调用者负责复用 SQLite graph runner 与项目注册表。"""

    app = FastAPI(title="RepoPilot Guard", version="0.1.0")
    store = task_store or TaskStore(registry.database_path)
    plugins = plugin_registry or PluginRegistry(registry.database_path)
    app.state.task_store = store
    create_mcp_runtime = mcp_runtime_factory or (lambda configuration, root: McpRuntime(configuration, workspace_root=root))
    index_document = document_indexer or (lambda project_id, source: index_uploaded_document(registry, project_id, source))
    check_runtime = runtime_health_checks or (lambda: ())
    # 当前 FastAPI/Starlette 版本通过 Router 生命周期关闭 SQLite 连接。
    app.router.on_shutdown.append(store.close)
    if plugin_registry is None:
        app.router.on_shutdown.append(plugins.close)
    # 开发期前端运行在 Vite 的独立本机端口，生产权限仍由 Python 后端裁决。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:1420",
            "http://localhost:1420",
            # Tauri 2 在生产桌面壳中以本机自定义协议加载页面。
            "http://tauri.localhost",
            "https://tauri.localhost",
        ],
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
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
            "dependencies": [check.to_dict() for check in checks],
        }

    @app.get("/api/projects")
    def list_projects() -> dict[str, object]:
        return {"projects": [item.to_dict() for item in registry.list()]}

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

    @app.get("/api/projects/{project_id}/diagnostics")
    def project_diagnostics(project_id: str) -> dict[str, object]:
        """只读返回项目是否满足两种任务模式与 Java/Maven Profile 的前置条件。"""

        try:
            return diagnose_project(registry.get(project_id))
        except ValueError as error:
            raise HTTPException(404, {"code": "PROJECT_NOT_FOUND", "message": "项目不存在。"}) from error

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
            grant = _grant_for_mode(body.task_mode, body.confirmation)
            request = TaskRequest(
                repository=repository,
                description=body.description,
                output_root=Path(body.output_root) if body.output_root else default_output_root,
                project_id=body.project_id,
                workspace_selection=WorkspaceSelection(mode=body.task_mode.workspace_mode),
                approved_mcp_tools=tuple(body.approved_mcp_tools),
            )
            thread_id = body.thread_id or str(uuid4())
            store.create(
                thread_id=thread_id,
                task_id=request.task_id,
                project_id=body.project_id,
                repository=repository,
                output_root=request.output_root,
                task_mode=body.task_mode.value,
                permission_mode=grant.mode.value,
                workspace_mode=request.workspace_selection.mode.value,
                display_title=body.description,
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
                except Exception as error:
                    # 不把异常细节或环境变量返回给桌面端；图自身的 BLOCKED 事件仍在 checkpoint 中。
                    try:
                        if store.get(thread_id).cancellation_requested_at:
                            store.complete_cancellation(thread_id)
                        else:
                            store.mark_runtime_failure(thread_id, f"TASK_RUNTIME_FAILED: {type(error).__name__}")
                    except ValueError:
                        return
                finally:
                    heartbeat_stop.set()
                    heartbeat.join(timeout=1)

            Thread(target=run_in_background, name=f"repopilot-{request.task_id}", daemon=True).start()
            return _task_snapshot(runner, store, thread_id)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    @app.get("/api/tasks")
    def list_tasks(
        limit: int = Query(default=50, ge=1, le=200),
        include_archived: bool = Query(default=False),
    ) -> dict[str, object]:
        store.reap_expired_leases()
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
                except Exception as error:
                    try:
                        if store.get(thread_id).cancellation_requested_at:
                            store.complete_cancellation(thread_id)
                        else:
                            store.mark_runtime_failure(thread_id, f"TASK_RUNTIME_FAILED: {type(error).__name__}")
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
            return task.to_dict()
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.delete("/api/tasks/{thread_id}")
    def archive_task(thread_id: str) -> dict[str, object]:
        try:
            return {"task": store.archive(thread_id).to_dict()}
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

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
                if snapshot.get("status") in {"REPORT", "BLOCKED", "CANCELLED"}:
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
        }

    @app.get("/api/tasks/{thread_id}/artifacts")
    def list_task_artifacts(thread_id: str) -> dict[str, object]:
        try:
            _task_snapshot(runner, store, thread_id)
            return {"artifacts": [item.to_dict() for item in store.artifacts(thread_id)]}
        except (KeyError, ValueError) as error:
            raise HTTPException(404, "TASK_ARTIFACTS_NOT_FOUND") from error

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


def _task_snapshot(
    runner: GraphRunner,
    task_store: TaskStore,
    thread_id: str,
) -> dict[str, object]:
    """优先读取 checkpoint 并同步索引；首个 checkpoint 前返回持久化任务记录。"""

    task_store.reap_expired_leases()
    persisted = task_store.get(thread_id)
    if persisted.lease_expires_at or persisted.cancellation_requested_at or persisted.error_summary == "TASK_LEASE_EXPIRED":
        return persisted.to_dict()
    try:
        result = runner.get(thread_id).to_dict()
        indexed = task_store.sync_graph_result(result, execution_finished=False)
        result.update(
            {
                "trace_id": indexed.trace_id,
                "task_id": indexed.task_id,
                "display_title": indexed.display_title,
                "project_id": indexed.project_id,
                "task_mode": indexed.task_mode,
                "created_at": indexed.created_at,
                "updated_at": indexed.updated_at,
                "archived_at": indexed.archived_at,
            }
        )
        return result
    except (KeyError, ValueError):
        return task_store.get(thread_id).to_dict()


def _event_cursor(after_sequence: int, last_event_id: str | None) -> int:
    if not last_event_id:
        return after_sequence
    try:
        return max(after_sequence, int(last_event_id.rsplit(":", 1)[1]))
    except (IndexError, ValueError):
        return after_sequence


def _sse_event(event: StoredTaskEvent) -> str:
    event_name = "state" if event.event_type == "TASK_STATE" else "evidence"
    payload = {
        "sequence": event.sequence,
        "event_id": event.event_id,
        "trace_id": event.trace_id,
        "type": event.event_type,
        **event.payload,
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


def _grant_for_mode(mode: TaskMode, confirmation: str | None) -> PermissionGrant:
    if mode is TaskMode.SAFE_ISOLATED:
        return PermissionGrant.safe()
    if confirmation != FULL_ACCESS_CONFIRMATION:
        raise ValueError("FULL_ACCESS_CONFIRMATION_REQUIRED")
    return PermissionGrant(PermissionMode.FULL, confirmation)
