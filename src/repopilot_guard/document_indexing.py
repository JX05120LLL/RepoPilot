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
        settings = AppSettings()
    except ValidationError:
        check = sanitized_settings_error()
        return {"status": "BLOCKED", "code": check.code, "message": check.message}

    provider = OpenAICompatibleProvider(settings)
    embedding_check = provider.embedding_check()
    if not embedding_check.ready:
        return {
            "status": "BLOCKED",
            "code": embedding_check.code,
            "message": embedding_check.message,
            "missing_fields": list(embedding_check.missing_fields),
        }
    try:
        project = registry.get(project_id)
        repo_commit = GitClient().head_commit(project.root_path)
        # CLI/API 可以显式指定状态库；文档副本和索引清单必须跟随同一项目注册表。
        state_path = registry.database_path
        documents = ManagedDocumentStore(state_path)
        document = documents.import_document(source, project_id=project.project_id)
        chunks = documents.chunks_for(document, project_id=project.project_id, repo_commit=repo_commit)
    except (ValueError, GitCommandError) as error:
        code = str(error)
        if not (code.startswith("DOCUMENT_") or code == "UNSUPPORTED_DOCUMENT_TYPE"):
            code = "DOCUMENT_INDEX_INPUT_INVALID"
        return {"status": "BLOCKED", "code": code, "message": "研发文档不可导入或项目 Git 基线不可用。"}

    try:
        bootstrapper = QdrantBootstrapper.from_settings(settings)
        health = bootstrapper.health_check()
        if not health.ready:
            return {"status": "BLOCKED", "code": health.code, "message": health.message}
        # 初始化幂等，绝不删除已有向量。
        bootstrapper.bootstrap()
        store = ContextChunkStore(state_path)
        try:
            result = ContextIndexer(bootstrapper.client, provider.create_embeddings(), store).index(chunks)
        finally:
            store.close()
    except Exception as error:
        return {
            "status": "BLOCKED",
            "code": "DOCUMENT_INDEX_DEPENDENCY_FAILED",
            "message": "文档索引依赖不可用，未报告为成功。",
            "failure_reason": type(error).__name__,
        }
    return {**result.to_dict(), "document": document.to_dict(), "repo_commit": repo_commit}
