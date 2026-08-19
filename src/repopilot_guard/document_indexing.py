"""用户研发文档的受控导入与 RAG 索引服务。"""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from repopilot_guard.config import AppSettings, sanitized_settings_error
from repopilot_guard.context import ContextChunkStore, ContextIndexer, ManagedDocumentStore
from repopilot_guard.project_registry import ProjectRegistry
from repopilot_guard.providers import OpenAICompatibleProvider
from repopilot_guard.qdrant_bootstrap import QdrantBootstrapper
from repopilot_guard.workspace import GitClient, GitCommandError


def index_uploaded_document(registry: ProjectRegistry, project_id: str, source: Path) -> dict[str, object]:
    """导入用户显式选择的文档并索引；失败时返回稳定 `BLOCKED` 结果。"""

    try:
        project = registry.get(project_id)
        state_path = registry.database_path
        documents = ManagedDocumentStore(state_path)
        document = documents.import_document(source, project_id=project.project_id)
    except ValueError as error:
        code = str(error)
        if not (code.startswith("DOCUMENT_") or code == "UNSUPPORTED_DOCUMENT_TYPE"):
            code = "DOCUMENT_INDEX_INPUT_INVALID"
        return {"status": "BLOCKED", "code": code, "message": "研发文档不可导入或不符合安全限制。"}

    # 导入本身独立于 RAG：普通对话可立即引用受控副本，向量索引只是可选增强。
    try:
        repo_commit = GitClient().head_commit(project.root_path)
    except GitCommandError:
        return {
            "status": "READY",
            "code": "DOCUMENT_IMPORTED_WITHOUT_RAG",
            "message": "文档已安全导入，可用于对话附件；项目 Git 基线不可用，因此未写入 RAG。",
            "document": document.to_dict(),
            "repo_commit": None,
        }

    try:
        settings = AppSettings()
    except ValidationError:
        check = sanitized_settings_error()
        return {
            "status": "READY",
            "code": "DOCUMENT_IMPORTED_WITHOUT_RAG",
            "message": "文档已安全导入，可用于对话附件；Embedding 配置不可用，因此未写入 RAG。",
            "document": document.to_dict(),
            "repo_commit": repo_commit,
            "rag_reason": check.code,
        }

    provider = OpenAICompatibleProvider(settings)
    embedding_check = provider.embedding_check()
    if not embedding_check.ready:
        return {
            "status": "READY",
            "code": "DOCUMENT_IMPORTED_WITHOUT_RAG",
            "message": "文档已安全导入，可用于对话附件；Embedding 配置不可用，因此未写入 RAG。",
            "document": document.to_dict(),
            "repo_commit": repo_commit,
            "rag_reason": embedding_check.code,
        }
    chunks = documents.chunks_for(document, project_id=project.project_id, repo_commit=repo_commit)

    try:
        bootstrapper = QdrantBootstrapper.from_settings(settings)
        health = bootstrapper.health_check()
        if not health.ready:
            return {
                "status": "READY",
                "code": "DOCUMENT_IMPORTED_WITHOUT_RAG",
                "message": "研发文档已受控导入，但向量检索暂不可用；可在普通对话中直接附加该文档。",
                "document": document.to_dict(),
                "repo_commit": repo_commit,
                "rag_reason": health.code,
            }
        # 初始化幂等，绝不删除已有向量。
        bootstrapper.bootstrap()
        store = ContextChunkStore(state_path)
        try:
            result = ContextIndexer(bootstrapper.client, provider.create_embeddings(), store).index(chunks)
        finally:
            store.close()
    except Exception as error:
        return {
            "status": "READY",
            "code": "DOCUMENT_IMPORTED_WITHOUT_RAG",
            "message": "文档已安全导入，可用于对话附件；RAG 依赖不可用，因此未写入向量库。",
            "document": document.to_dict(),
            "repo_commit": repo_commit,
            "rag_reason": type(error).__name__,
        }
    return {**result.to_dict(), "document": document.to_dict(), "repo_commit": repo_commit}
