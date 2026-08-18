"""阶段四 Step 2：graph.py 拆分出的子模块（由 graph.py 兼容 shim 统一重导出）。"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver

from repopilot_guard.config import AppSettings, ComponentCheck
from repopilot_guard.context import (
    ContextChunkStore,
    ContextIndexer,
    ContextLoader,
    ContextRetriever,
    ManagedDocumentStore,
    ProjectMemoryRetriever,
    VerifiedProjectMemoryWriter,
)
from repopilot_guard.mcp_agent import TaskMcpBindingService
from repopilot_guard.providers import OpenAICompatibleProvider
from repopilot_guard.qdrant_bootstrap import QdrantBootstrapper
from repopilot_guard.shell_runtime import ShellRuntime

from .context_services import ContextService, LiveContextService, NoopContextService, NoopProjectMemoryWriter, ProjectMemoryWriter
from .factory import CodingGraphFactory
from .preflight import PhaseOnePreflightChecker
from .research_model import NoopResearchModel, OpenAIResearchModel, ResearchModel

def create_live_graph(settings: AppSettings, checkpointer: SqliteSaver) -> Any:
    """组装真实 Provider/Qdrant 依赖；配置不完整时由 PREFLIGHT 返回 BLOCKED。"""

    provider = OpenAICompatibleProvider(settings)
    preflight = PhaseOnePreflightChecker(settings)
    context_service: ContextService = NoopContextService()
    research_model: ResearchModel = NoopResearchModel()
    project_memory_writer: ProjectMemoryWriter = NoopProjectMemoryWriter()
    configuration_ready = all(
        check.ready
        for check in (
            settings.chat_check(),
            settings.embedding_check(),
            settings.qdrant_bootstrap_check(),
        )
    )
    if configuration_ready:
        try:
            bootstrapper = QdrantBootstrapper.from_settings(settings)
            embeddings = provider.create_embeddings()
            context_store = ContextChunkStore(settings.state_db_path)
            context_service = LiveContextService(
                ContextLoader(),
                ContextIndexer(bootstrapper.client, embeddings, context_store),
                ContextRetriever(bootstrapper.client, embeddings, context_store),
                ProjectMemoryRetriever(bootstrapper.client, embeddings),
                ManagedDocumentStore(settings.state_db_path),
            )
            research_model = OpenAIResearchModel(provider, fallback_model=provider.create_fallback_chat_model())
            project_memory_writer = VerifiedProjectMemoryWriter(bootstrapper.client, embeddings)
        except (TypeError, ValueError):
            preflight = PhaseOnePreflightChecker(
                settings,
                dependency_setup_check=ComponentCheck(
                    component="agent_dependencies",
                    ready=False,
                    code="DEPENDENCY_INITIALIZATION_FAILED",
                    message="Agent 依赖初始化失败，未暴露内部配置或密钥。",
                ),
            )
    # 同一注册表同时约束插件 Skill 与 MCP 快照，避免两条能力路径对插件
    # 启用状态、包版本或完整性得出不同结论。
    from repopilot_guard.context_broker import ContextBroker
    from repopilot_guard.hooks import HookRuntime
    from repopilot_guard.plugins import PluginRegistry

    plugin_registry = PluginRegistry(settings.state_db_path)

    return CodingGraphFactory(
        preflight,
        context_service=context_service,
        research_model=research_model,
        project_memory_writer=project_memory_writer,
        context_broker=ContextBroker(
            plugin_registry=plugin_registry,
            user_skill_roots=settings.user_skill_roots,
            bundled_skill_roots=settings.bundled_skill_roots,
        ),
        mcp_binding_service=TaskMcpBindingService(plugin_registry=plugin_registry),
        shell_runtime=ShellRuntime(enabled=settings.full_local_shell_enabled),
        hook_runtime=HookRuntime(plugin_registry),
    ).create(checkpointer)
