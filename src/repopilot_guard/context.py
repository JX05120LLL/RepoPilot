"""代码与研发文档的受控加载、切分、Qdrant 索引和检索。"""

from __future__ import annotations

import hashlib
import json
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

from repopilot_guard.document_parser import SUPPORTED_DOCUMENT_EXTENSIONS, extract_document_text
from repopilot_guard.permissions import PermissionGrant
from repopilot_guard.policy import PolicyGuard, ToolName


MAX_FILE_BYTES = 256 * 1024
MAX_CHUNK_CHARACTERS = 1200
CHUNK_OVERLAP_CHARACTERS = 200
EMBEDDING_BATCH_SIZE = 10
MAX_RETRIEVAL_CANDIDATES = 64
# 一项任务最多绑定 4 份文档，每份先保证一个片段进入 Context Broker。
# 其余内容仍可由受限的 retrieve_context 按需检索，不让第一份长文档挖占所有显式附件预算。
MAX_ATTACHED_DOCUMENT_CHUNKS = 1
TRANSIENT_OPERATION_ATTEMPTS = 3
TRANSIENT_RETRY_BASE_DELAY_SECONDS = 1.0
CODE_EXTENSIONS = frozenset({".java", ".xml", ".py", ".js", ".jsx", ".ts", ".tsx", ".gradle", ".kts"})
DOCUMENT_EXTENSIONS = SUPPORTED_DOCUMENT_EXTENSIONS
BUILD_DESCRIPTOR_NAMES = frozenset(
    {
        "pom.xml",
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "pyproject.toml",
        "pytest.ini",
        "requirements.txt",
        "settings.gradle",
        "settings.gradle.kts",
    }
)
CONFIGURATION_FILE_PATTERN = re.compile(r"^(?:application(?:-[a-z0-9_.-]+)?|config)\.(?:json|ya?ml)$", re.IGNORECASE)
SENSITIVE_CONFIGURATION_KEYS = frozenset(
    {
        "accesskey",
        "accesstoken",
        "apikey",
        "authorization",
        "clientsecret",
        "credential",
        "credentials",
        "password",
        "passwd",
        "privatekey",
        "refreshtoken",
        "secret",
        "token",
    }
)
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
    symbols: tuple[str, ...] = ()

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
            "symbols": list(self.symbols),
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
    bm25_score: float | None = None
    symbols: tuple[str, ...] = ()
    symbol_score: float = 0.0

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
            "bm25_score": self.bm25_score,
            "symbols": list(self.symbols),
            "symbol_score": self.symbol_score,
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
    """保存向量增量状态，并在本机 SQLite 中维护受控 BM25 倒排索引。"""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path.expanduser().resolve()
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lexical_available = False
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
            try:
                # 元数据列不参与分词，只能作为项目和提交隔离过滤条件使用。
                connection.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS context_lexical_chunks USING fts5(
                        chunk_id UNINDEXED,
                        project_id UNINDEXED,
                        repo_commit UNINDEXED,
                        source_type UNINDEXED,
                        path UNINDEXED,
                        document_id UNINDEXED,
                        line_start UNINDEXED,
                        line_end UNINDEXED,
                        symbols,
                        content
                    )
                    """
                )
                self._lexical_available = True
            except sqlite3.OperationalError:
                # 少数 Python/SQLite 构建可能不带 FTS5；向量检索仍可用，不能伪造 BM25 已启用。
                self._lexical_available = False
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

    @property
    def lexical_available(self) -> bool:
        """本机 SQLite 是否支持并已初始化 FTS5。"""

        return self._lexical_available

    def is_lexically_indexed(self, chunk: ContextChunk) -> bool:
        if not self._lexical_available:
            return False
        connection = sqlite3.connect(self._database_path)
        try:
            row = connection.execute(
                "SELECT 1 FROM context_lexical_chunks WHERE chunk_id = ? LIMIT 1", (chunk.chunk_id,)
            ).fetchone()
        finally:
            connection.close()
        return row is not None

    def index_lexical(self, chunks: list[ContextChunk]) -> None:
        """以 chunk_id 替换写入，避免同一代码片段在 BM25 索引中重复。"""

        if not self._lexical_available or not chunks:
            return
        connection = sqlite3.connect(self._database_path)
        try:
            connection.executemany(
                "DELETE FROM context_lexical_chunks WHERE chunk_id = ?",
                [(chunk.chunk_id,) for chunk in chunks],
            )
            connection.executemany(
                """
                INSERT INTO context_lexical_chunks(
                    chunk_id, project_id, repo_commit, source_type, path, document_id,
                    line_start, line_end, symbols, content
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        chunk.chunk_id,
                        chunk.project_id,
                        chunk.repo_commit,
                        chunk.source_type,
                        chunk.path,
                        chunk.document_id,
                        chunk.line_start,
                        chunk.line_end,
                        " ".join(chunk.symbols),
                        chunk.content,
                    )
                    for chunk in chunks
                ],
            )
            connection.commit()
        finally:
            connection.close()

    def search_lexical(
        self,
        query: str,
        *,
        project_id: str,
        repo_commit: str,
        limit: int,
    ) -> tuple[RetrievedContext, ...]:
        """只在已冻结的项目和提交内执行 FTS5 BM25 查询。"""

        terms = _fts_query_terms(query)
        if not self._lexical_available or not terms or limit < 1:
            return ()
        # 仅由受限分词函数生成 MATCH 表达式，避免把用户输入解释为 FTS 语法。
        match_query = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)
        connection = sqlite3.connect(self._database_path)
        try:
            rows = connection.execute(
                """
                SELECT chunk_id, source_type, path, document_id, line_start, line_end, symbols, content,
                       bm25(context_lexical_chunks) AS bm25_rank
                FROM context_lexical_chunks
                WHERE context_lexical_chunks MATCH ?
                  AND project_id = ?
                  AND repo_commit = ?
                ORDER BY bm25_rank ASC, path ASC, line_start ASC, line_end ASC
                LIMIT ?
                """,
                (match_query, project_id, repo_commit, limit),
            ).fetchall()
        finally:
            connection.close()
        total = len(rows)
        return tuple(
            RetrievedContext(
                content=str(row[7]),
                score=0.0,
                path=str(row[2]),
                line_start=int(row[4]),
                line_end=int(row[5]),
                source_type=str(row[1]),
                document_id=str(row[3]),
                bm25_score=round(1.0 - (rank / max(total, 1)), 6),
                symbols=tuple(item for item in str(row[6]).split(" ") if item),
            )
            for rank, row in enumerate(rows)
        )

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
    source_format: str = "text"

    def to_dict(self) -> dict[str, object]:
        return {
            "document_id": self.document_id,
            "display_name": self.display_name,
            "content_sha256": self.content_sha256,
            "imported_at": self.imported_at,
            "source_format": self.source_format,
        }


@dataclass(frozen=True, slots=True)
class AttachedDocumentContextResult:
    """任务级文档附件的受控快照。

    它不依赖向量相似度命中，因此用户明确添加的文档不会被静默忽略。
    """

    status: str
    code: str
    message: str
    contexts: tuple[RetrievedContext, ...] = ()
    documents: tuple[dict[str, str], ...] = ()
    truncated: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "documents": [dict(item) for item in self.documents],
            "context_count": len(self.contexts),
            "truncated": self.truncated,
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
                    source_format TEXT NOT NULL DEFAULT 'text',
                    PRIMARY KEY(project_id, document_id)
                )
                """
            )
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(managed_documents)").fetchall()}
            if "source_format" not in columns:
                connection.execute("ALTER TABLE managed_documents ADD COLUMN source_format TEXT NOT NULL DEFAULT 'text'")
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
        suffix = source.suffix.lower()
        if suffix not in DOCUMENT_EXTENSIONS:
            raise ValueError("UNSUPPORTED_DOCUMENT_TYPE")
        try:
            if not source.is_file():
                raise ValueError("DOCUMENT_UNREADABLE")
            raw = source.read_bytes()
        except OSError as error:
            raise ValueError("DOCUMENT_UNREADABLE") from error
        try:
            text = _redact_configuration_secrets(extract_document_text(raw, suffix))
        except ValueError as error:
            raise ValueError(str(error)) from error

        raw_sha256 = hashlib.sha256(raw).hexdigest()
        normalized = text.encode("utf-8")
        content_sha256 = hashlib.sha256(normalized).hexdigest()
        document_id = hashlib.sha256(
            f"managed-document|{project_id}|{source.name}|{raw_sha256}".encode("utf-8")
        ).hexdigest()
        display_name = _safe_document_name(source.name, suffix)
        target_directory = self._documents_root / hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:24]
        target = target_directory / f"{document_id}{'.md' if suffix == '.md' else '.txt'}"
        target_directory.mkdir(parents=True, exist_ok=True)
        try:
            if not target.exists():
                target.write_bytes(normalized)
            elif hashlib.sha256(target.read_bytes()).hexdigest() != content_sha256:
                raise ValueError("MANAGED_DOCUMENT_INTEGRITY_FAILED")
        except OSError as error:
            raise ValueError("DOCUMENT_STORAGE_FAILED") from error

        imported_at = datetime_now_iso()
        connection = sqlite3.connect(self._database_path)
        try:
            connection.execute(
                """
                INSERT INTO managed_documents(project_id, document_id, display_name, managed_path, content_sha256, imported_at, source_format)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, document_id) DO UPDATE SET
                    display_name=excluded.display_name,
                    managed_path=excluded.managed_path,
                    content_sha256=excluded.content_sha256,
                    imported_at=excluded.imported_at,
                    source_format=excluded.source_format
                """,
                (project_id, document_id, display_name, str(target), content_sha256, imported_at, suffix.lstrip(".")),
            )
            connection.commit()
        finally:
            connection.close()
        return ManagedDocument(document_id, display_name, target, content_sha256, imported_at, suffix.lstrip("."))

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

    def resolve_for_task(
        self,
        *,
        project_id: str,
        repo_commit: str,
        document_ids: tuple[str, ...],
    ) -> AttachedDocumentContextResult:
        """将已经受控导入的文档转为当前任务的强制上下文。

        用于模型的路径只是稳定显示名，从不返回用户的源文件路径或受管理的副本路径。
        """

        try:
            documents = self.require_documents(project_id=project_id, document_ids=document_ids)
            contexts: list[RetrievedContext] = []
            truncated = False
            for document in documents:
                chunks = self.chunks_for(document, project_id=project_id, repo_commit=repo_commit)
                selected = chunks[:MAX_ATTACHED_DOCUMENT_CHUNKS]
                truncated = truncated or len(chunks) > len(selected)
                contexts.extend(
                    RetrievedContext(
                        content=chunk.content,
                        score=1.0,
                        path=chunk.path,
                        line_start=chunk.line_start,
                        line_end=chunk.line_end,
                        source_type="task_attachment",
                        document_id=document.document_id,
                    )
                    for chunk in selected
                )
            return AttachedDocumentContextResult(
                "READY",
                "TASK_ATTACHMENTS_READY",
                "用户显式附加的研发文档已冻结到当前任务上下文。",
                tuple(contexts),
                tuple(
                    {
                        "document_id": document.document_id,
                        "display_name": document.display_name,
                        "content_sha256": document.content_sha256,
                    }
                    for document in documents
                ),
                truncated,
            )
        except ValueError as error:
            return AttachedDocumentContextResult(
                "BLOCKED",
                str(error),
                "任务附件不可用、归属不匹配或完整性校验失败。",
            )

    def require_documents(self, *, project_id: str, document_ids: tuple[str, ...]) -> tuple[ManagedDocument, ...]:
        """将文档 ID 绑定到项目；未知、跨项目或重复 ID 均按阻断处理。"""

        if not project_id.strip():
            raise ValueError("TASK_ATTACHMENTS_REQUIRE_PROJECT")
        if len(document_ids) > 4:
            raise ValueError("TASK_ATTACHMENTS_LIMIT_EXCEEDED")
        if len(set(document_ids)) != len(document_ids):
            raise ValueError("TASK_ATTACHMENTS_DUPLICATE")
        if any(
            not isinstance(document_id, str)
            or len(document_id) != 64
            or any(character not in "0123456789abcdef" for character in document_id)
            for document_id in document_ids
        ):
            raise ValueError("TASK_ATTACHMENT_ID_INVALID")
        if not document_ids:
            return ()
        available = {document.document_id: document for document in self.list_documents(project_id=project_id)}
        missing = [document_id for document_id in document_ids if document_id not in available]
        if missing:
            raise ValueError("TASK_ATTACHMENT_NOT_FOUND")
        return tuple(available[document_id] for document_id in document_ids)

    def list_documents(self, *, project_id: str) -> tuple[ManagedDocument, ...]:
        """列出项目已导入文档；结果不包含用户最初选择的外部路径。"""

        if not project_id.strip():
            raise ValueError("DOCUMENT_PROJECT_INVALID")
        connection = sqlite3.connect(self._database_path)
        try:
            rows = connection.execute(
                """
                SELECT document_id, display_name, managed_path, content_sha256, imported_at, source_format
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
                source_format=str(row[5]),
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
                if not _is_allowed_project_file(path):
                    skipped_files += 1
                    continue
                if not guard.check_path(ToolName.READ_FILE, path).allowed:
                    skipped_files += 1
                    continue
                # 配置文件先在本地完成确定性脱敏，再允许其进入切块、Embedding 或本地倒排索引。
                text = _read_project_text(path)
                if text is None:
                    skipped_files += 1
                    continue
                source_type = _project_source_type(path)
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


def _is_allowed_project_file(path: Path) -> bool:
    """只放行源码、文档、构建描述和受治理的常见配置，避免宽泛索引 JSON/YAML 凭证。"""

    return (
        path.suffix.lower() in CODE_EXTENSIONS | DOCUMENT_EXTENSIONS
        or path.name in BUILD_DESCRIPTOR_NAMES
        or bool(CONFIGURATION_FILE_PATTERN.fullmatch(path.name))
    )


def _project_source_type(path: Path) -> str:
    if path.name in BUILD_DESCRIPTOR_NAMES or path.suffix.lower() in {".gradle", ".kts"}:
        return "build_config"
    if CONFIGURATION_FILE_PATTERN.fullmatch(path.name):
        return "configuration"
    return "code" if path.suffix.lower() in CODE_EXTENSIONS else "repository_document"


def _read_project_text(path: Path) -> str | None:
    """读取项目文件，并在配置内容离开本机文件系统前移除常见凭证值。"""

    text = _read_text(path)
    if text is None:
        return None
    if path.suffix.lower() == ".json":
        return _redact_json_configuration_secrets(text)
    if path.suffix.lower() in {".yaml", ".yml"}:
        return _redact_configuration_secrets(text)
    return text


def _redact_json_configuration_secrets(text: str) -> str:
    """优先解析 JSON 并递归脱敏，嵌套凭证对象也不能保留任何子字段。"""

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # 用户项目中可能存在带注释的 JSONC；无法可靠解析时仍保守按行脱敏，
        # 不因为格式不标准而回退为原文索引。
        return _redact_configuration_secrets(text)
    sanitized = _redact_json_value(parsed)
    return json.dumps(sanitized, ensure_ascii=False, indent=2) + "\n"


def _redact_json_value(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _is_sensitive_configuration_key(str(key)) else _redact_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_json_value(item) for item in value]
    if isinstance(value, str):
        return _redact_connection_credentials(value)
    return value


def _redact_configuration_secrets(text: str) -> str:
    """对 JSON/YAML 的敏感键和值做保守脱敏，不依赖宽松解析器或外部库。"""

    lines: list[str] = []
    blocked_block_indent: int | None = None
    for line in text.splitlines():
        indentation = len(line) - len(line.lstrip(" "))
        if blocked_block_indent is not None:
            if line.strip() and indentation <= blocked_block_indent:
                blocked_block_indent = None
            elif not line.strip() or indentation > blocked_block_indent:
                lines.append(" " * indentation + "# [REDACTED]")
                continue

        json_match = re.match(
            r'^(?P<prefix>\s*"(?P<key>[^\"]+)"\s*:\s*)(?P<value>.*?)(?P<suffix>,?\s*)$',
            line,
        )
        if json_match and _is_sensitive_configuration_key(json_match.group("key")):
            lines.append(f'{json_match.group("prefix")}"[REDACTED]"{json_match.group("suffix")}')
            continue

        key_match = re.match(
            r"^(?P<prefix>\s*(?:-\s*)?[\"']?(?P<key>[A-Za-z0-9_.-]+)[\"']?\s*:\s*)(?P<value>.*)$",
            line,
        )
        if key_match and _is_sensitive_configuration_key(key_match.group("key")):
            value = key_match.group("value").strip()
            if value.startswith(("|", ">")):
                blocked_block_indent = indentation
            lines.append(f"{key_match.group('prefix')}[REDACTED]")
            continue

        # 连接串中的用户信息也不能以普通配置值的形式进入上下文。
        lines.append(_redact_connection_credentials(line))
    return "\n".join(lines)


def _redact_connection_credentials(value: str) -> str:
    return re.sub(r"(://)[^\s/@:]+:[^\s/@]+@", r"\1[REDACTED]@", value)


def _is_sensitive_configuration_key(value: str) -> bool:
    """只匹配明确的键名或最后一级键，避免把 tokenizer 一类正常字段误判为密钥。"""

    key = value.rsplit(".", 1)[-1]
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return normalized in SENSITIVE_CONFIGURATION_KEYS


class ContextIndexer:
    """将受控 chunk 写入 coding_context，失败不制造成功结果。"""

    def __init__(self, client: Any, embeddings: EmbeddingsClient, chunk_store: ContextChunkStore | None = None) -> None:
        self._client = client
        self._embeddings = embeddings
        self._chunk_store = chunk_store

    def index(self, chunks: tuple[ContextChunk, ...], skipped_files: int = 0) -> IndexResult:
        vector_pending = [chunk for chunk in chunks if not self._chunk_store or not self._chunk_store.is_current(chunk)]
        lexical_pending = (
            [chunk for chunk in chunks if not self._chunk_store.is_lexically_indexed(chunk)]
            if self._chunk_store and self._chunk_store.lexical_available
            else []
        )
        if not vector_pending and not lexical_pending:
            return IndexResult("READY", "CONTEXT_ALREADY_INDEXED", "上下文没有变化，无需重复写入。", skipped_chunks=len(chunks), skipped_files=skipped_files)
        for batch in _batches(vector_pending, EMBEDDING_BATCH_SIZE):
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
                self._chunk_store.mark_indexed(vector_pending)
                self._chunk_store.index_lexical(lexical_pending)
        except sqlite3.Error as error:
            return IndexResult(
                "BLOCKED",
                "CONTEXT_INDEX_FAILED",
                "向量状态或 BM25 索引写入失败，未报告为完整成功。",
                skipped_files=skipped_files,
                failure_component="state_database",
                failure_reason=type(error).__name__,
            )
        return IndexResult(
            "READY",
            "CONTEXT_INDEXED",
            "代码与文档上下文已索引。",
            indexed_chunks=len(vector_pending),
            skipped_chunks=len(chunks) - len(vector_pending),
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
    """只在同一项目和基线提交内融合向量、BM25、关键词、路径和符号信号。"""

    def __init__(self, client: Any, embeddings: EmbeddingsClient, chunk_store: ContextChunkStore | None = None) -> None:
        self._client = client
        self._embeddings = embeddings
        self._chunk_store = chunk_store

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
        by_source: dict[tuple[str, int, int], int] = {}
        for vector_rank, point in enumerate(response.points):
            payload = point.payload or {}
            key = (str(payload.get("path", "")), int(payload.get("line_start", 0)), int(payload.get("line_end", 0)))
            if key in by_source:
                continue
            by_source[key] = len(candidates)
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
                        symbols=_payload_symbols(payload),
                    ),
                    vector_rank,
                )
            )
        lexical_candidates: tuple[RetrievedContext, ...] = ()
        if self._chunk_store and self._chunk_store.lexical_available:
            try:
                lexical_candidates = self._chunk_store.search_lexical(
                    query,
                    project_id=project_id,
                    repo_commit=repo_commit,
                    limit=candidate_limit,
                )
            except sqlite3.Error:
                # 本地增强索引不可用时仍保留 Qdrant 的严格项目/提交隔离检索。
                lexical_candidates = ()
        for lexical_rank, lexical_context in enumerate(lexical_candidates):
            key = (lexical_context.path, lexical_context.line_start, lexical_context.line_end)
            existing_index = by_source.get(key)
            if existing_index is None:
                by_source[key] = len(candidates)
                candidates.append((lexical_context, candidate_limit + lexical_rank))
                continue
            vector_context, vector_rank = candidates[existing_index]
            candidates[existing_index] = (
                RetrievedContext(
                    content=vector_context.content,
                    score=vector_context.score,
                    path=vector_context.path,
                    line_start=vector_context.line_start,
                    line_end=vector_context.line_end,
                    source_type=vector_context.source_type,
                    document_id=vector_context.document_id,
                    vector_score=vector_context.vector_score,
                    bm25_score=lexical_context.bm25_score,
                    symbols=vector_context.symbols or lexical_context.symbols,
                ),
                vector_rank,
            )
        contexts = _hybrid_rerank(query, candidates, limit)
        uses_bm25 = bool(self._chunk_store and self._chunk_store.lexical_available)
        return RetrievalResult(
            "READY",
            "CONTEXT_RETRIEVED",
            "上下文检索完成。",
            contexts,
            len(response.points) >= candidate_limit,
            strategy="hybrid_vector_bm25_lexical_symbol_path" if uses_bm25 else "hybrid_vector_lexical_symbol_path",
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
        semantic_score = 1.0 - (vector_rank / max(candidate_count, 1)) if context.vector_score is not None else 0.0
        lexical_score = max(_lexical_relevance(query, context), context.bm25_score or 0.0)
        symbol_score = _symbol_relevance(query, context.symbols)
        hybrid_score = round(semantic_score * 0.3 + lexical_score * 0.4 + symbol_score * 0.3, 6)
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
                    bm25_score=context.bm25_score,
                    symbols=context.symbols,
                    symbol_score=symbol_score,
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


_JAVA_TYPE_PATTERN = re.compile(r"\b(?:class|interface|enum|record)\s+([A-Za-z_$][A-Za-z0-9_$]*)")
_JAVA_METHOD_PATTERN = re.compile(
    r"(?m)^\s*(?:(?:public|protected|private|static|final|abstract|synchronized|native|default)\s+)*"
    r"(?:[A-Za-z_$][A-Za-z0-9_$<>?,.\[\]\s]*\s+)([A-Za-z_$][A-Za-z0-9_$]*)\s*\("
)
_JAVA_NON_METHOD_NAMES = frozenset({"if", "for", "while", "switch", "catch", "return", "throw", "new"})


def _java_symbols(content: str) -> tuple[str, ...]:
    """提取切块内稳定的 Java 声明名，不尝试构建完整 AST 或调用关系。"""

    values = [*_JAVA_TYPE_PATTERN.findall(content)]
    values.extend(name for name in _JAVA_METHOD_PATTERN.findall(content) if name not in _JAVA_NON_METHOD_NAMES)
    return tuple(sorted(dict.fromkeys(values)))[:32]


def _payload_symbols(payload: dict[str, object]) -> tuple[str, ...]:
    values = payload.get("symbols")
    if not isinstance(values, list):
        return ()
    return tuple(symbol for symbol in values if isinstance(symbol, str) and symbol)[:32]


def _symbol_relevance(query: str, symbols: tuple[str, ...]) -> float:
    terms = _query_terms(query)
    if not terms or not symbols:
        return 0.0
    normalized = {symbol.lower() for symbol in symbols}
    compact_query = re.sub(r"[^a-z0-9_$]", "", query.lower())
    if compact_query and compact_query in normalized:
        return 1.0
    return round(sum(1 for term in terms if term in normalized) / len(terms), 6)


def _is_safe_memory_path(path: str) -> bool:
    """记忆路径来自补丁执行结果，仍做最小结构校验避免污染长期索引。"""

    normalized = path.replace("\\", "/").strip()
    return bool(normalized and not normalized.startswith("/") and ".." not in normalized.split("/"))


def _query_terms(query: str) -> tuple[str, ...]:
    """保留中英文/类名关键词，避免将单字符噪声当成检索信号。"""
    normalized = re.sub(r"([a-z])([A-Z])", r"\1 \2", query).lower()
    terms = re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", normalized)
    return tuple(dict.fromkeys(terms))


def _fts_query_terms(query: str) -> tuple[str, ...]:
    """同时保留原始标识符与 camelCase 拆分词，避免 FTS 漏掉精确类名。"""

    raw_terms = re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", query.lower())
    return tuple(dict.fromkeys((*raw_terms, *_query_terms(query))))


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
                        symbols=_java_symbols(content) if path.lower().endswith(".java") else (),
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
