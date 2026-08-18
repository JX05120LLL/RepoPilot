"""阶段四 Step 2：graph.py 拆分出的子模块（由 graph.py 兼容 shim 统一重导出）。"""

from __future__ import annotations

from typing import Protocol

from repopilot_guard.context import (
    AttachedDocumentContextResult,
    ContextIndexer,
    ContextLoader,
    ContextRetriever,
    IndexResult,
    ManagedDocumentStore,
    ProjectMemoryResult,
    ProjectMemoryRetriever,
    RetrievalResult,
)
from repopilot_guard.permissions import PermissionGrant

from .states import GraphWorkspaceContext

class ContextService(Protocol):
    def ingest(self, workspace: GraphWorkspaceContext, project_id: str, permission: PermissionGrant) -> IndexResult: ...

    def retrieve(self, query: str, project_id: str, repo_commit: str) -> RetrievalResult: ...


class LiveContextService:
    """阶段三索引、检索能力的图内适配。"""

    def __init__(
        self,
        loader: ContextLoader,
        indexer: ContextIndexer,
        retriever: ContextRetriever,
        memory_retriever: ProjectMemoryRetriever | None = None,
        managed_documents: ManagedDocumentStore | None = None,
    ) -> None:
        self._loader = loader
        self._indexer = indexer
        self._retriever = retriever
        self._memory_retriever = memory_retriever
        self._managed_documents = managed_documents

    def ingest(self, workspace: GraphWorkspaceContext, project_id: str, permission: PermissionGrant) -> IndexResult:
        chunks, skipped = self._loader.load_project(
            workspace.workspace_path,
            project_id=project_id,
            repo_commit=workspace.base_commit,
            permission=permission,
        )
        return self._indexer.index(chunks, skipped)

    def retrieve(self, query: str, project_id: str, repo_commit: str) -> RetrievalResult:
        code_result = self._retriever.search(query, project_id=project_id, repo_commit=repo_commit, limit=6)
        if code_result.status != "READY" or self._memory_retriever is None:
            return code_result
        memory_result = self._memory_retriever.search(query, project_id=project_id, limit=2)
        if memory_result.status != "READY":
            return RetrievalResult(
                "READY",
                "CONTEXT_RETRIEVED_WITH_MEMORY_WARNING",
                "当前提交上下文检索完成；已验证项目记忆暂不可用。",
                code_result.contexts,
                code_result.truncated,
                strategy=code_result.strategy,
                candidate_count=code_result.candidate_count,
            )
        return RetrievalResult(
            "READY",
            "CONTEXT_RETRIEVED_WITH_PROJECT_MEMORY",
            "当前提交上下文与同项目已验证记忆检索完成。",
            tuple([*memory_result.contexts, *code_result.contexts]),
            memory_result.truncated or code_result.truncated,
            strategy="current_commit_hybrid_plus_verified_project_memory",
            candidate_count=memory_result.candidate_count + code_result.candidate_count,
        )

    def task_attachments(
        self,
        project_id: str,
        repo_commit: str,
        document_ids: tuple[str, ...],
    ) -> AttachedDocumentContextResult:
        """返回当前任务显式绑定的文档片段，与 Qdrant 排序结果互不替代。"""

        if self._managed_documents is None:
            return AttachedDocumentContextResult(
                "BLOCKED",
                "TASK_ATTACHMENTS_UNAVAILABLE",
                "当前运行未配置受控研发文档存储，不能忽略任务附件继续执行。",
            )
        return self._managed_documents.resolve_for_task(
            project_id=project_id,
            repo_commit=repo_commit,
            document_ids=document_ids,
        )


class ProjectMemoryWriter(Protocol):
    def record(
        self,
        *,
        project_id: str,
        task_id: str,
        repo_commit: str,
        changed_paths: tuple[str, ...],
        git_diff: str,
        verification: dict[str, object],
    ) -> ProjectMemoryResult: ...


class NoopProjectMemoryWriter:
    """测试和未配置真实 Qdrant 时不写入任何长期状态。"""

    def record(
        self,
        *,
        project_id: str,
        task_id: str,
        repo_commit: str,
        changed_paths: tuple[str, ...],
        git_diff: str,
        verification: dict[str, object],
    ) -> ProjectMemoryResult:
        return ProjectMemoryResult("READY", "PROJECT_MEMORY_SKIPPED", "当前运行未配置项目长期记忆写入器。")


class NoopContextService:
    """供旧图测试使用；真实 CLI 一律注入 LiveContextService。"""

    def ingest(self, workspace: GraphWorkspaceContext, project_id: str, permission: PermissionGrant) -> IndexResult:
        return IndexResult("READY", "CONTEXT_INGEST_SKIPPED", "测试运行未配置真实上下文服务。")

    def retrieve(self, query: str, project_id: str, repo_commit: str) -> RetrievalResult:
        return RetrievalResult("READY", "CONTEXT_NOT_FOUND", "未配置真实上下文服务。")
