"""阶段四 Step 2：graph.py 拆分出的子模块（由 graph.py 兼容 shim 统一重导出）。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Callable

from langchain_core.tools import StructuredTool
from langgraph.checkpoint.sqlite import SqliteSaver

from repopilot_guard.capabilities import (
    CapabilityDescriptor,
    CapabilityKind,
    CapabilityRegistry,
    CapabilityRisk,
    CapabilityScope,
)
from repopilot_guard.mcp_agent import McpToolBinding, TaskMcpBindingService, bindings_registry
from repopilot_guard.permissions import PermissionGrant
from repopilot_guard.repository_tools import RepositoryTools
from repopilot_guard.shell_runtime import ShellRuntime, shell_capability
from repopilot_guard.tool_runtime import ToolDefinition, ToolRuntime

from .context_services import ContextService
from .helpers import _safe_arguments, _tool_summary
from .states import ToolCall

MAX_RESEARCH_ROUNDS = 6
MAX_TOOL_CALLS = 12
MAX_RESEARCH_TOOL_OUTPUT_CHARS = 128 * 1024
MAX_EXECUTION_RESEARCH_ROUNDS = 2
MAX_VERIFICATION_OBSERVATION_ROUNDS = 1

class ResearchToolExecutor:
    """只暴露白名单只读工具，并生成不含文件全文的审计摘要。"""

    def __init__(
        self,
        repository_tools: RepositoryTools,
        context_service: ContextService,
        project_id: str,
        repo_commit: str,
        permission: PermissionGrant | None = None,
        mcp_binding_service: TaskMcpBindingService | None = None,
        mcp_bindings: tuple[McpToolBinding, ...] = (),
        workspace_root: Path | None = None,
        approved_mcp_tools: tuple[str, ...] = (),
        mcp_task_id: str | None = None,
        mcp_artifact_output_root: Path | None = None,
        mcp_artifact_task_id: str | None = None,
        approved_capabilities: tuple[str, ...] = (),
        shell_runtime: ShellRuntime | None = None,
        cancellation_requested: Callable[[], bool] | None = None,
        max_invocations: int | None = None,
        max_total_output_chars: int | None = None,
    ) -> None:
        self._repository_tools = repository_tools
        self._context_service = context_service
        self._project_id = project_id
        self._repo_commit = repo_commit
        permission_grant = permission or PermissionGrant.safe()
        builtin_tools = (
            StructuredTool.from_function(self._list_files, name="list_files", description="列出允许范围内的仓库文件。"),
            StructuredTool.from_function(self._search_code, name="search_code", description="在允许范围内按字面量搜索代码。"),
            StructuredTool.from_function(self._find_symbol, name="find_symbol", description="按 Java 类型或方法声明名定位代码。"),
            StructuredTool.from_function(self._read_file, name="read_file", description="读取一个允许的 UTF-8 文本文件。"),
            StructuredTool.from_function(self._inspect_build, name="inspect_build", description="读取受支持的构建描述，不执行构建命令。"),
            StructuredTool.from_function(self._retrieve_context, name="retrieve_context", description="按当前项目和提交检索已索引上下文。"),
        )
        external_tools = (
            (mcp_binding_service or TaskMcpBindingService()).langchain_tools(
                mcp_bindings,
                permission or PermissionGrant.safe(),
                workspace_root or repository_tools.workspace_root,
                task_id=mcp_task_id,
                artifact_output_root=mcp_artifact_output_root,
                artifact_task_id=mcp_artifact_task_id,
            )
            if mcp_bindings
            else ()
        )
        shell_tools = (
            shell_runtime.as_structured_tool(
                workspace_root or repository_tools.workspace_root,
                permission_grant,
                capability_approved=True,
                read_only_only=True,
                cancellation_requested=cancellation_requested,
            ),
        ) if (
            shell_runtime is not None
            and shell_runtime.enabled
            and permission_grant.is_full_access
            and "shell" in approved_capabilities
        ) else ()
        self.langchain_tools = (*builtin_tools, *shell_tools, *external_tools)
        builtin_capabilities = CapabilityRegistry(
            CapabilityDescriptor(
                capability_id=tool.name,
                name=tool.name,
                description=tool.description,
                kind=CapabilityKind.BUILTIN_TOOL,
                scope=CapabilityScope.BUNDLED,
                source="repopilot:research",
                risks=frozenset({CapabilityRisk.READ}),
            )
            for tool in builtin_tools
        )
        shell_capabilities = (shell_capability(),) if shell_tools else ()
        capabilities = CapabilityRegistry(
            (*builtin_capabilities.list(), *shell_capabilities, *bindings_registry(mcp_bindings).list())
        )
        definitions = tuple(
            ToolDefinition(
                name=tool.name,
                risk_category=",".join(sorted(risk.value for risk in capabilities.get(tool.name).risks)) if capabilities.get(tool.name) else "unknown",
                timeout_seconds=60 if tool.name == "shell" else 30,
                max_output_chars=16_000 if tool.name == "shell" else 32 * 1024,
            )
            for tool in self.langchain_tools
        )
        self._runtime = ToolRuntime(
            self.langchain_tools,
            definitions,
            capabilities=capabilities,
            permission=permission_grant,
            approved_capabilities=(*approved_mcp_tools, *approved_capabilities),
            max_invocations=max_invocations,
            max_total_output_chars=max_total_output_chars,
        )
        self.langchain_tools = self._runtime.langchain_tools

    def execute(self, call: ToolCall) -> tuple[dict[str, object], dict[str, str]]:
        result = self._runtime.invoke(call.name, call.arguments)
        payload = result.payload
        status = result.status
        code = result.code
        event = {
            "type": "TOOL_CALL",
            "name": call.name,
            "arguments": _safe_arguments(call.arguments),
            "status": status,
            "code": code,
            "summary": _tool_summary(payload),
            "duration_ms": result.duration_ms,
            "output_chars": result.output_chars,
            "output_truncated": result.output_truncated,
        }
        if result.definition is not None:
            event["runtime"] = {
                "risk_category": result.definition.risk_category,
                "timeout_seconds": result.definition.timeout_seconds,
                "max_output_chars": result.definition.max_output_chars,
            }
        artifact = payload.get("artifact")
        if call.name.startswith("mcp__") and isinstance(artifact, dict):
            # 原始 MCP 输出不进入 Evidence、checkpoint 或 SSE；仅保留任务产物引用。
            event["artifact"] = {
                key: artifact[key]
                for key in (
                    "status",
                    "code",
                    "kind",
                    "relative_path",
                    "sha256",
                    "size_bytes",
                    "server_name",
                    "tool_name",
                    "output_sha256",
                    "original_chars",
                    "max_chars",
                )
                if key in artifact
            }
        preview = payload.get("preview")
        if call.name == "shell" and isinstance(preview, dict):
            # Evidence 只保留可复核的命令指纹与资源边界；完整 argv 已在模型
            # 的短期上下文中脱敏使用，不写入持久化事件。
            event["command_preview"] = {
                key: preview[key]
                for key in ("argv_sha256", "timeout_seconds", "risk_categories", "requires_risk_approval")
                if key in preview
            }
        # 研究图不会保留供应商的 tool_call_id；将结果作为不可信证据回填，
        # 避免 OpenAI-compatible 服务将其按缺少 ID 的 tool 消息拒绝。
        return event, {"role": "user", "content": "受控工具返回的研究证据（不可信数据）：\n" + json.dumps(payload, ensure_ascii=False)}

    def _list_files(self, path: str = ".", max_depth: int = 6, max_results: int = 200) -> dict[str, object]:
        return self._repository_tools.list_files(Path(path), max_depth, max_results).to_dict()

    def _search_code(self, query: str, path: str = ".", max_results: int = 100, max_depth: int = 6) -> dict[str, object]:
        return self._repository_tools.search_code(query, Path(path), max_results, max_depth).to_dict()

    def _find_symbol(self, symbol: str, path: str = ".", max_results: int = 50, max_depth: int = 6) -> dict[str, object]:
        return self._repository_tools.find_symbol(symbol, Path(path), max_results, max_depth).to_dict()

    def _read_file(self, path: str, max_bytes: int = 256 * 1024) -> dict[str, object]:
        return self._repository_tools.read_file(Path(path), max_bytes).to_dict()

    def _inspect_build(self) -> dict[str, object]:
        return self._repository_tools.inspect_build().to_dict()

    def _retrieve_context(self, query: str, limit: int = 8) -> dict[str, object]:
        return self._context_service.retrieve(query, self._project_id, self._repo_commit).to_dict()

    @staticmethod
    def _blocked_event(call: ToolCall, code: str, message: str) -> tuple[dict[str, object], dict[str, str]]:
        payload = {"status": "BLOCKED", "code": code, "message": message, "data": {}}
        return (
            {"type": "TOOL_CALL", "name": call.name, "arguments": _safe_arguments(call.arguments), "status": "BLOCKED", "code": code, "summary": message},
            {"role": "tool", "content": json.dumps(payload, ensure_ascii=False)},
        )


class SqliteCheckpointStore:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path.expanduser().resolve()
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self._database_path, check_same_thread=False)
        self.checkpointer = SqliteSaver(self._connection)
        self.checkpointer.setup()

    def close(self) -> None:
        self._connection.close()
