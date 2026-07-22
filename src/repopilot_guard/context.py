"""代码与研发文档的受控加载、切分、Qdrant 索引和检索。"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol, TypeVar
from uuid import UUID

from openai import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError
from qdrant_client import models

from repopilot_guard.permissions import PermissionGrant
from repopilot_guard.policy import PolicyGuard, ToolName


MAX_FILE_BYTES = 256 * 1024
MAX_CHUNK_CHARACTERS = 1200
CHUNK_OVERLAP_CHARACTERS = 200
EMBEDDING_BATCH_SIZE = 10
MAX_RETRIEVAL_CANDIDATES = 64
TRANSIENT_OPERATION_ATTEMPTS = 3
TRANSIENT_RETRY_BASE_DELAY_SECONDS = 1.0
CODE_EXTENSIONS = frozenset({".java", ".xml"})
DOCUMENT_EXTENSIONS = frozenset({".md", ".txt"})
SKIPPED_DIRECTORIES = frozenset({"target", ".venv", "node_modules", "build", ".idea"})


class EmbeddingsClient(Protocol):
    """LangChain Embeddings 的最小运行时能力。"""

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


@dataclass(frozen=True, slots=True)
class ContextChunk:
    """一个带可引用来源的确定性上下文片段。"""

    chunk_id: str
    content: str
    project_id: str
    repo_commit: str
    source_type: str
    path: str
    document_id: str
    line_start: int
    line_end: int
    content_sha256: str
    verified: bool = False

    def payload(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "repo_commit": self.repo_commit,
            "source_type": self.source_type,
            "path": self.path,
            "document_id": self.document_id,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "content_sha256": self.content_sha256,
            "verified": self.verified,
            "content": self.content,
        }


@dataclass(frozen=True, slots=True)
class IndexResult:
    status: str
    code: str
    message: str
    indexed_chunks: int = 0
    skipped_chunks: int = 0
    skipped_files: int = 0
    failure_component: str | None = None
    failure_reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "indexed_chunks": self.indexed_chunks,
            "skipped_chunks": self.skipped_chunks,
            "skipped_files": self.skipped_files,
            "failure_component": self.failure_component,
            "failure_reason": self.failure_reason,
        }


@dataclass(frozen=True, slots=True)
class ProjectMemoryResult:
    """项目长期记忆的受控写入结果，不改变代码修复本身的验证结论。"""

    status: str
    code: str
    message: str
    recorded_facts: int = 0
    failure_component: str | None = None
    failure_reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "recorded_facts": self.recorded_facts,
            "failure_component": self.failure_component,
            "failure_reason": self.failure_reason,
        }


@dataclass(frozen=True, slots=True)
class RetrievedContext:
    content: str
    score: float
    path: str
    line_start: int
    line_end: int
    source_type: str
    document_id: str
    vector_score: float | None = None
    lexical_score: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "content": self.content,
            "score": self.score,
            "path": self.path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "source_type": self.source_type,
            "document_id": self.document_id,
            "vector_score": self.vector_score,
            "lexical_score": self.lexical_score,
        }


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    status: str
    code: str
    message: str
    contexts: tuple[RetrievedContext, ...] = ()
    truncated: bool = False
    failure_component: str | None = None
    failure_reason: str | None = None
    strategy: str = "vector"
    candidate_count: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "contexts": [item.to_dict() for item in self.contexts],
            "truncated": self.truncated,
            "failure_component": self.failure_component,
            "failure_reason": self.failure_reason,
            "strategy": self.strategy,
            "candidate_count": self.candidate_count,
        }


class ContextChunkStore:
    """保存已成功 upsert 的 chunk 哈希，避免重复向量写入。"""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path.expanduser().resolve()
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._database_path)
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS context_chunks (
                    chunk_id TEXT PRIMARY KEY,
                    content_sha256 TEXT NOT NULL
                )
                """
            )
            connection.commit()
        finally:
            connection.close()

    def is_current(self, chunk: ContextChunk) -> bool:
        connection = sqlite3.connect(self._database_path)
        try:
            row = connection.execute(
                "SELECT content_sha256 FROM context_chunks WHERE chunk_id = ?", (chunk.chunk_id,)
            ).fetchone()
        finally:
            connection.close()
        return bool(row and row[0] == chunk.content_sha256)

    def mark_indexed(self, chunks: list[ContextChunk]) -> None:
        connection = sqlite3.connect(self._database_path)
        try:
            connection.executemany(
                "INSERT OR REPLACE INTO context_chunks(chunk_id, content_sha256) VALUES (?, ?)",
                [(chunk.chunk_id, chunk.content_sha256) for chunk in chunks],
            )
            connection.commit()
        finally:
            connection.close()

    def close(self) -> None:
        """兼容旧调用；索引清单使用短连接，不保留文件句柄。"""


@dataclass(frozen=True, slots=True)
class ManagedDocument:
    """用户显式导入到 RepoPilot 状态目录的研发文档元数据。"""

    document_id: str
    display_name: str
    managed_path: Path
    content_sha256: str
    imported_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "document_id": self.document_id,
            "display_name": self.display_name,
            "content_sha256": self.content_sha256,
            "imported_at": self.imported_at,
        }


class ManagedDocumentStore:
    """保存用户主动选择的 MD/TXT 副本，不修改用户仓库或保留源文件绝对路径。"""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path.expanduser().resolve()
        self._documents_root = self._database_path.parent / "documents"
        self._documents_root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._database_path)
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS managed_documents (
                    project_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    managed_path TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    imported_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, document_id)
                )
                """
            )
            connection.commit()
        finally:
            connection.close()

    def import_document(self, source_path: Path, *, project_id: str) -> ManagedDocument:
        """复制经用户选择的文档到应用目录；不把源路径持久化到 RAG Payload。"""

        if not project_id.strip():
            raise ValueError("DOCUMENT_PROJECT_INVALID")
        source_candidate = source_path.expanduser()
        if source_candidate.is_symlink():
            raise ValueError("DOCUMENT_SYMLINK_BLOCKED")
        source = source_candidate.resolve()
        if source.suffix.lower() not in DOCUMENT_EXTENSIONS:
            raise ValueError("UNSUPPORTED_DOCUMENT_TYPE")
        try:
            if not source.is_file():
                raise ValueError("DOCUMENT_UNREADABLE")
            raw = source.read_bytes()
        except OSError as error:
            raise ValueError("DOCUMENT_UNREADABLE") from error
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError("DOCUMENT_TOO_LARGE")
        if b"\0" in raw:
            raise ValueError("DOCUMENT_UNREADABLE")
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("DOCUMENT_UNREADABLE") from error

        content_sha256 = hashlib.sha256(raw).hexdigest()
        document_id = hashlib.sha256(
            f"managed-document|{project_id}|{source.name}|{content_sha256}".encode("utf-8")
        ).hexdigest()
        display_name = _safe_document_name(source.name, source.suffix.lower())
        target_directory = self._documents_root / hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:24]
        target = target_directory / f"{document_id}{source.suffix.lower()}"
        target_directory.mkdir(parents=True, exist_ok=True)
        try:
            if not target.exists():
                target.write_bytes(raw)
            elif hashlib.sha256(target.read_bytes()).hexdigest() != content_sha256:
                raise ValueError("MANAGED_DOCUMENT_INTEGRITY_FAILED")
        except OSError as error:
            raise ValueError("DOCUMENT_STORAGE_FAILED") from error

        imported_at = datetime_now_iso()
        connection = sqlite3.connect(self._database_path)
        try:
            connection.execute(
                """
                INSERT INTO managed_documents(project_id, document_id, display_name, managed_path, content_sha256, imported_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, document_id) DO UPDATE SET
                    display_name=excluded.display_name,
                    managed_path=excluded.managed_path,
                    content_sha256=excluded.content_sha256,
                    imported_at=excluded.imported_at
                """,
                (project_id, document_id, display_name, str(target), content_sha256, imported_at),
            )
            connection.commit()
        finally:
            connection.close()
        return ManagedDocument(document_id, display_name, target, content_sha256, imported_at)

    def chunks_for(
        self,
        document: ManagedDocument,
        *,
        project_id: str,
        repo_commit: str,
    ) -> tuple[ContextChunk, ...]:
        """从受控副本切分；Payload 只引用 RepoPilot 管理的稳定显示名。"""

        try:
            raw = document.managed_path.read_bytes()
        except OSError as error:
            raise ValueError("MANAGED_DOCUMENT_UNAVAILABLE") from error
        if hashlib.sha256(raw).hexdigest() != document.content_sha256:
            raise ValueError("MANAGED_DOCUMENT_INTEGRITY_FAILED")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("MANAGED_DOCUMENT_UNAVAILABLE") from error
        return split_context(
            text,
            project_id=project_id,
            repo_commit=repo_commit,
            source_type="uploaded_document",
            path=f"uploaded_documents/{document.display_name}",
            document_id=document.document_id,
            markdown=document.managed_path.suffix.lower() == ".md",
        )

    def list_documents(self, *, project_id: str) -> tuple[ManagedDocument, ...]:
        """列出项目已导入文档；结果不包含用户最初选择的外部路径。"""

        if not project_id.strip():
            raise ValueError("DOCUMENT_PROJECT_INVALID")
        connection = sqlite3.connect(self._database_path)
        try:
            rows = connection.execute(
                """
                SELECT document_id, display_name, managed_path, content_sha256, imported_at
                FROM managed_documents WHERE project_id = ? ORDER BY imported_at DESC
                """,
                (project_id,),
            ).fetchall()
        finally:
            connection.close()
        return tuple(
            ManagedDocument(
                document_id=str(row[0]),
                display_name=str(row[1]),
                managed_path=Path(str(row[2])),
                content_sha256=str(row[3]),
                imported_at=str(row[4]),
            )
            for row in rows
        )


class ContextLoader:
    """只加载项目内通过策略校验的代码和文本文件。"""

    def load_project(
        self,
        root: Path,
        *,
        project_id: str,
        repo_commit: str,
        permission: PermissionGrant,
    ) -> tuple[tuple[ContextChunk, ...], int]:
        project_root = root.expanduser().resolve()
        guard = PolicyGuard(project_root, permission)
        chunks: list[ContextChunk] = []
        skipped_files = 0
        for directory, directories, filenames in os.walk(project_root, topdown=True, followlinks=False):
            current = Path(directory)
            directories[:] = sorted(
                name
                for name in directories
                if name not in SKIPPED_DIRECTORIES and guard.check_path(ToolName.READ_FILE, current / name).allowed
            )
            for filename in sorted(filenames):
                path = current / filename
                relative = path.relative_to(project_root).as_posix()
                if path.suffix.lower() not in CODE_EXTENSIONS | DOCUMENT_EXTENSIONS:
                    skipped_files += 1
                    continue
                if not guard.check_path(ToolName.READ_FILE, path).allowed:
                    skipped_files += 1
                    continue
                text = _read_text(path)
                if text is None:
                    skipped_files += 1
                    continue
                source_type = "code" if path.suffix.lower() in CODE_EXTENSIONS else "repository_document"
                chunks.extend(
                    split_context(
                        text,
                        project_id=project_id,
                        repo_commit=repo_commit,
                        source_type=source_type,
                        path=relative,
                        document_id=_document_id(relative),
                        markdown=path.suffix.lower() == ".md",
                    )
                )
        return tuple(chunks), skipped_files

    def load_document(
        self,
        document_path: Path,
        *,
        project_root: Path,
        project_id: str,
        repo_commit: str,
        permission: PermissionGrant,
    ) -> tuple[ContextChunk, ...]:
        document = document_path.expanduser().resolve()
        guard = PolicyGuard(project_root.expanduser().resolve(), permission)
        if document.suffix.lower() not in DOCUMENT_EXTENSIONS:
            raise ValueError("UNSUPPORTED_DOCUMENT_TYPE")
        if not guard.check_path(ToolName.READ_FILE, document).allowed:
            raise ValueError("DOCUMENT_PATH_BLOCKED")
        text = _read_text(document)
        if text is None:
            raise ValueError("DOCUMENT_UNREADABLE")
        return split_context(
            text,
            project_id=project_id,
            repo_commit=repo_commit,
            source_type="uploaded_document",
            path=str(document),
            document_id=_document_id(str(document)),
            markdown=document.suffix.lower() == ".md",
        )


class ContextIndexer:
    """将受控 chunk 写入 coding_context，失败不制造成功结果。"""

    def __init__(self, client: Any, embeddings: EmbeddingsClient, chunk_store: ContextChunkStore | None = None) -> None:
        self._client = client
        self._embeddings = embeddings
        self._chunk_store = chunk_store

    def index(self, chunks: tuple[ContextChunk, ...], skipped_files: int = 0) -> IndexResult:
        pending = [chunk for chunk in chunks if not self._chunk_store or not self._chunk_store.is_current(chunk)]
        if not pending:
            return IndexResult("READY", "CONTEXT_ALREADY_INDEXED", "上下文没有变化，无需重复写入。", skipped_chunks=len(chunks), skipped_files=skipped_files)
        for batch in _batches(pending, EMBEDDING_BATCH_SIZE):
            try:
                vectors = _with_transient_retry(lambda: self._embeddings.embed_documents([chunk.content for chunk in batch]))
                points = [
                    models.PointStruct(id=_qdrant_point_id(chunk.chunk_id), vector=vector, payload=chunk.payload())
                    for chunk, vector in zip(batch, vectors, strict=True)
                ]
            except Exception as error:
                return IndexResult(
                    "BLOCKED",
                    "CONTEXT_INDEX_FAILED",
                    "Embedding 生成失败，未报告为成功。",
                    skipped_files=skipped_files,
                    failure_component="embedding",
                    failure_reason=type(error).__name__,
                )
            try:
                self._client.upsert(collection_name="coding_context", points=points, wait=True)
            except Exception as error:
                return IndexResult(
                    "BLOCKED",
                    "CONTEXT_INDEX_FAILED",
                    "Qdrant 写入失败，未报告为成功。",
                    skipped_files=skipped_files,
                    failure_component="qdrant",
                    failure_reason=type(error).__name__,
                )
        try:
            if self._chunk_store:
                self._chunk_store.mark_indexed(pending)
        except sqlite3.Error as error:
            return IndexResult(
                "BLOCKED",
                "CONTEXT_INDEX_FAILED",
                "索引状态写入失败，未报告为成功。",
                skipped_files=skipped_files,
                failure_component="state_database",
                failure_reason=type(error).__name__,
            )
        return IndexResult(
            "READY",
            "CONTEXT_INDEXED",
            "代码与文档上下文已索引。",
            indexed_chunks=len(pending),
            skipped_chunks=len(chunks) - len(pending),
            skipped_files=skipped_files,
        )


class VerifiedProjectMemoryWriter:
    """仅将真实 Diff 与 Maven 成功共同证明的变更事实写入 project_memory。"""

    def __init__(self, client: Any, embeddings: EmbeddingsClient) -> None:
        self._client = client
        self._embeddings = embeddings

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
        if (
            not project_id.strip()
            or not task_id.strip()
            or not repo_commit.strip()
            or not git_diff.strip()
            or verification.get("status") != "PASSED"
        ):
            return ProjectMemoryResult(
                "BLOCKED",
                "PROJECT_MEMORY_UNVERIFIED",
                "项目记忆只接受已通过 Diff 与 Maven 验证的事实。",
            )
        paths = tuple(sorted({path for path in changed_paths if _is_safe_memory_path(path)}))
        if not paths:
            return ProjectMemoryResult(
                "BLOCKED",
                "PROJECT_MEMORY_NO_CHANGED_PATHS",
                "项目记忆必须关联真实修改文件，未写入泛化结论。",
            )

        diff_sha256 = hashlib.sha256(git_diff.encode("utf-8")).hexdigest()
        verification_sha256 = hashlib.sha256(
            repr(sorted((str(key), str(value)) for key, value in verification.items())).encode("utf-8")
        ).hexdigest()
        recipe = str(verification.get("recipe") or "unknown")
        exit_code = verification.get("exit_code")
        contents = [
            (
                f"已验证项目变更事实：文件 {path} 在基线提交 {repo_commit} 的任务 {task_id} 中被修改；"
                f"固定 Maven 配方 {recipe} 已通过（exit_code={exit_code}）。"
            )
            for path in paths
        ]
        try:
            vectors = _with_transient_retry(lambda: self._embeddings.embed_documents(contents))
        except Exception as error:
            return ProjectMemoryResult(
                "BLOCKED",
                "PROJECT_MEMORY_INDEX_FAILED",
                "项目记忆 Embedding 失败，未伪造记录成功。",
                failure_component="embedding",
                failure_reason=type(error).__name__,
            )
        points = []
        for path, content, vector in zip(paths, contents, vectors, strict=True):
            memory_id = hashlib.sha256(
                f"verified-memory|{project_id}|{task_id}|{repo_commit}|{path}|{diff_sha256}|{verification_sha256}".encode("utf-8")
            ).hexdigest()
            content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
            points.append(
                models.PointStruct(
                    id=_qdrant_point_id(memory_id),
                    vector=vector,
                    payload={
                        "project_id": project_id,
                        "repo_commit": repo_commit,
                        "source_type": "verified_project_memory",
                        "path": path,
                        "document_id": task_id,
                        "line_start": 0,
                        "line_end": 0,
                        "content_sha256": content_sha256,
                        "verified": True,
                        "task_id": task_id,
                        "diff_sha256": diff_sha256,
                        "verification_sha256": verification_sha256,
                        "verification_recipe": recipe,
                        "verification_exit_code": exit_code,
                        "content": content,
                    },
                )
            )
        try:
            self._client.upsert(collection_name="project_memory", points=points, wait=True)
        except Exception as error:
            return ProjectMemoryResult(
                "BLOCKED",
                "PROJECT_MEMORY_INDEX_FAILED",
                "Qdrant 项目记忆写入失败，未伪造记录成功。",
                failure_component="qdrant",
                failure_reason=type(error).__name__,
            )
        return ProjectMemoryResult(
            "READY",
            "PROJECT_MEMORY_RECORDED",
            "已记录通过真实 Diff 与 Maven 验证的项目变更事实。",
            recorded_facts=len(points),
        )


class ProjectMemoryRetriever:
    """只读检索同一项目的已验证记忆，可跨基线提交但始终携带原始提交来源。"""

    def __init__(self, client: Any, embeddings: EmbeddingsClient) -> None:
        self._client = client
        self._embeddings = embeddings

    def search(self, query: str, *, project_id: str, limit: int = 2) -> RetrievalResult:
        if not query.strip() or not project_id.strip() or limit < 1:
            return RetrievalResult("BLOCKED", "INVALID_PROJECT_MEMORY_QUERY", "项目记忆检索参数无效。")
        try:
            query_vector = _with_transient_retry(lambda: self._embeddings.embed_query(query))
        except Exception as error:
            return RetrievalResult(
                "BLOCKED",
                "PROJECT_MEMORY_RETRIEVAL_FAILED",
                "项目记忆检索向量生成失败，未返回猜测结果。",
                failure_component="embedding",
                failure_reason=type(error).__name__,
            )
        try:
            response = self._client.query_points(
                collection_name="project_memory",
                query=query_vector,
                query_filter=models.Filter(
                    must=[
                        models.FieldCondition(key="project_id", match=models.MatchValue(value=project_id)),
                        models.FieldCondition(key="verified", match=models.MatchValue(value=True)),
                    ]
                ),
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as error:
            return RetrievalResult(
                "BLOCKED",
                "PROJECT_MEMORY_RETRIEVAL_FAILED",
                "Qdrant 项目记忆检索失败，未返回猜测结果。",
                failure_component="qdrant",
                failure_reason=type(error).__name__,
            )
        contexts = tuple(
            RetrievedContext(
                content=str(point.payload.get("content", "")) if point.payload else "",
                score=float(point.score),
                path=str(point.payload.get("path", "")) if point.payload else "",
                line_start=int(point.payload.get("line_start", 0)) if point.payload else 0,
                line_end=int(point.payload.get("line_end", 0)) if point.payload else 0,
                source_type="verified_project_memory",
                document_id=str(point.payload.get("document_id", "")) if point.payload else "",
                vector_score=float(point.score),
            )
            for point in response.points
            if point.payload and point.payload.get("verified") is True
        )
        return RetrievalResult(
            "READY",
            "PROJECT_MEMORY_RETRIEVED",
            "已检索同一项目的已验证长期记忆。",
            contexts,
            len(response.points) >= limit,
            strategy="verified_project_memory_vector",
            candidate_count=len(contexts),
        )


class ContextRetriever:
    """只在同一项目和基线提交内检索，并以向量、关键词和路径信号稳定重排。"""

    def __init__(self, client: Any, embeddings: EmbeddingsClient) -> None:
        self._client = client
        self._embeddings = embeddings

    def search(self, query: str, *, project_id: str, repo_commit: str, limit: int = 8) -> RetrievalResult:
        if not query.strip() or limit < 1:
            return RetrievalResult("BLOCKED", "INVALID_CONTEXT_QUERY", "检索内容和数量上限必须有效。")
        try:
            query_vector = _with_transient_retry(lambda: self._embeddings.embed_query(query))
        except Exception as error:
            return RetrievalResult(
                "BLOCKED",
                "CONTEXT_RETRIEVAL_FAILED",
                "检索向量生成失败，未返回猜测结果。",
                failure_component="embedding",
                failure_reason=type(error).__name__,
            )
        candidate_limit = min(MAX_RETRIEVAL_CANDIDATES, max(limit, limit * 4))
        try:
            response = self._client.query_points(
                collection_name="coding_context",
                query=query_vector,
                query_filter=models.Filter(
                    must=[
                        models.FieldCondition(key="project_id", match=models.MatchValue(value=project_id)),
                        models.FieldCondition(key="repo_commit", match=models.MatchValue(value=repo_commit)),
                    ]
                ),
                limit=candidate_limit,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as error:
            return RetrievalResult(
                "BLOCKED",
                "CONTEXT_RETRIEVAL_FAILED",
                "Qdrant 检索失败，未返回猜测结果。",
                failure_component="qdrant",
                failure_reason=type(error).__name__,
            )
        candidates: list[tuple[RetrievedContext, int]] = []
        seen: set[tuple[str, int, int]] = set()
        for vector_rank, point in enumerate(response.points):
            payload = point.payload or {}
            key = (str(payload.get("path", "")), int(payload.get("line_start", 0)), int(payload.get("line_end", 0)))
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                (
                    RetrievedContext(
                        content=str(payload.get("content", "")),
                        score=float(point.score),
                        path=str(payload.get("path", "")),
                        line_start=int(payload.get("line_start", 0)),
                        line_end=int(payload.get("line_end", 0)),
                        source_type=str(payload.get("source_type", "")),
                        document_id=str(payload.get("document_id", "")),
                        vector_score=float(point.score),
                    ),
                    vector_rank,
                )
            )
        contexts = _hybrid_rerank(query, candidates, limit)
        return RetrievalResult(
            "READY",
            "CONTEXT_RETRIEVED",
            "上下文检索完成。",
            contexts,
            len(response.points) >= candidate_limit,
            strategy="hybrid_vector_lexical_path",
            candidate_count=len(candidates),
        )


def _hybrid_rerank(
    query: str,
    candidates: list[tuple[RetrievedContext, int]],
    limit: int,
) -> tuple[RetrievedContext, ...]:
    """在已过滤候选集中融合向量排名、字面量和路径匹配，结果可稳定复现。"""
    if not candidates:
        return ()
    ranked: list[tuple[float, RetrievedContext]] = []
    candidate_count = len(candidates)
    for context, vector_rank in candidates:
        semantic_score = 1.0 - (vector_rank / max(candidate_count, 1))
        lexical_score = _lexical_relevance(query, context)
        hybrid_score = round(semantic_score * 0.45 + lexical_score * 0.55, 6)
        ranked.append(
            (
                hybrid_score,
                RetrievedContext(
                    content=context.content,
                    score=hybrid_score,
                    path=context.path,
                    line_start=context.line_start,
                    line_end=context.line_end,
                    source_type=context.source_type,
                    document_id=context.document_id,
                    vector_score=context.vector_score,
                    lexical_score=lexical_score,
                ),
            )
        )
    ranked.sort(key=lambda item: (-item[0], item[1].path, item[1].line_start, item[1].line_end))
    return tuple(item[1] for item in ranked[:limit])


def _lexical_relevance(query: str, context: RetrievedContext) -> float:
    tokens = _query_terms(query)
    if not tokens:
        return 0.0
    content = context.content.lower()
    path = context.path.lower()
    matches = 0.0
    for token in tokens:
        if token in path:
            matches += 1.0
        elif token in content:
            matches += 0.65
    score = matches / len(tokens)
    normalized_query = " ".join(tokens)
    if normalized_query and (normalized_query in path or normalized_query in content):
        score += 0.25
    return min(1.0, round(score, 6))


def _is_safe_memory_path(path: str) -> bool:
    """记忆路径来自补丁执行结果，仍做最小结构校验避免污染长期索引。"""

    normalized = path.replace("\\", "/").strip()
    return bool(normalized and not normalized.startswith("/") and ".." not in normalized.split("/"))


def _query_terms(query: str) -> tuple[str, ...]:
    """保留中英文/类名关键词，避免将单字符噪声当成检索信号。"""
    normalized = re.sub(r"([a-z])([A-Z])", r"\1 \2", query).lower()
    terms = re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", normalized)
    return tuple(dict.fromkeys(terms))


def split_context(
    text: str,
    *,
    project_id: str,
    repo_commit: str,
    source_type: str,
    path: str,
    document_id: str,
    markdown: bool,
) -> tuple[ContextChunk, ...]:
    """按确定字符预算切分，并记录每段的原始行范围。"""

    segments = _markdown_segments(text) if markdown else ((0, text),)
    chunks: list[ContextChunk] = []
    for segment_offset, segment in segments:
        start = 0
        while start < len(segment):
            end = min(len(segment), start + MAX_CHUNK_CHARACTERS)
            if end < len(segment):
                newline = segment.rfind("\n", start, end)
                if newline > start:
                    end = newline + 1
            content = segment[start:end].strip()
            if content:
                absolute_start = segment_offset + start
                line_start = text.count("\n", 0, absolute_start) + 1
                line_end = line_start + content.count("\n")
                content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                identity = f"{project_id}|{repo_commit}|{path}|{line_start}|{line_end}|{content_hash}"
                chunks.append(
                    ContextChunk(
                        chunk_id=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                        content=content,
                        project_id=project_id,
                        repo_commit=repo_commit,
                        source_type=source_type,
                        path=path,
                        document_id=document_id,
                        line_start=line_start,
                        line_end=line_end,
                        content_sha256=content_hash,
                    )
                )
            if end >= len(segment):
                break
            start = max(end - CHUNK_OVERLAP_CHARACTERS, start + 1)
    return tuple(chunks)


def _markdown_segments(text: str) -> tuple[tuple[int, str], ...]:
    starts = [0]
    offset = 0
    for line in text.splitlines(keepends=True):
        if line.startswith("#") and offset > 0:
            starts.append(offset)
        offset += len(line)
    starts.append(len(text))
    return tuple((starts[index], text[starts[index] : starts[index + 1]]) for index in range(len(starts) - 1))


def _read_text(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return None
        raw = path.read_bytes()
        if b"\0" in raw:
            return None
        return raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _document_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_document_name(value: str, suffix: str) -> str:
    """避免将控制字符或源目录信息带入模型可见的文档来源。"""

    stem = Path(value).stem
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._")
    return f"{(normalized or 'document')[:96]}{suffix}"


def datetime_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _batches(items: list[ContextChunk], size: int) -> tuple[list[ContextChunk], ...]:
    """限制单次 Embedding 请求体，兼容供应商的批量输入上限。"""
    return tuple(items[index : index + size] for index in range(0, len(items), size))


def _qdrant_point_id(chunk_id: str) -> str:
    """Qdrant 仅接受整数或 UUID；chunk_id 仍完整保存在 SQLite 与 Payload 语义中。"""
    return str(UUID(hex=chunk_id[:32]))


_ResultT = TypeVar("_ResultT")


def _with_transient_retry(operation: Callable[[], _ResultT]) -> _ResultT:
    """只重试明确可恢复的模型服务错误，不重试本地校验和编程错误。"""
    for attempt in range(TRANSIENT_OPERATION_ATTEMPTS):
        try:
            return operation()
        except (APIConnectionError, APITimeoutError, InternalServerError, RateLimitError):
            if attempt + 1 == TRANSIENT_OPERATION_ATTEMPTS:
                raise
            time.sleep(TRANSIENT_RETRY_BASE_DELAY_SECONDS * (2**attempt))
    raise RuntimeError("瞬时操作未返回结果")
