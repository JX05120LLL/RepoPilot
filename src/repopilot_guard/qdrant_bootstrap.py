"""Qdrant Collection 的幂等初始化与连通性检查。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from qdrant_client import QdrantClient, models

from repopilot_guard.config import AppSettings, ComponentCheck


COLLECTION_NAMES = ("coding_context", "project_memory")
PAYLOAD_INDEXES: dict[str, models.PayloadSchemaType] = {
    "project_id": models.PayloadSchemaType.KEYWORD,
    "repo_commit": models.PayloadSchemaType.KEYWORD,
    "source_type": models.PayloadSchemaType.KEYWORD,
    "path": models.PayloadSchemaType.KEYWORD,
    "document_id": models.PayloadSchemaType.KEYWORD,
    "content_sha256": models.PayloadSchemaType.KEYWORD,
    "verified": models.PayloadSchemaType.BOOL,
}


@dataclass(frozen=True)
class QdrantBootstrapResult:
    """记录本次初始化的新增资源，不暴露连接凭据。"""

    collections_created: tuple[str, ...]
    collections_existing: tuple[str, ...]
    indexes_created: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": "READY",
            "collections_created": list(self.collections_created),
            "collections_existing": list(self.collections_existing),
            "indexes_created": list(self.indexes_created),
        }


class QdrantBootstrapper:
    """只创建缺失的 Collection 与 Payload 索引，绝不删除已有向量。"""

    def __init__(self, client: Any, embedding_dimensions: int) -> None:
        if embedding_dimensions <= 0:
            raise ValueError("INVALID_EMBEDDING_DIMENSIONS")
        self._client = client
        self._embedding_dimensions = embedding_dimensions

    @classmethod
    def from_settings(cls, settings: AppSettings) -> "QdrantBootstrapper":
        check = settings.qdrant_bootstrap_check()
        if not check.ready:
            raise ValueError(check.code)
        return cls(_local_qdrant_client(settings.qdrant_url), settings.embedding_dimensions)

    def health_check(self) -> ComponentCheck:
        try:
            self._client.get_collections()
        except Exception:
            return ComponentCheck(
                component="qdrant",
                ready=False,
                code="QDRANT_UNAVAILABLE",
                message="无法连接 Qdrant，请确认 Docker Desktop 与 qdrant 服务已启动。",
            )
        return ComponentCheck(
            component="qdrant",
            ready=True,
            code="QDRANT_READY",
            message="Qdrant 可用。",
        )

    @property
    def client(self) -> Any:
        """暴露给索引与检索层的已配置客户端。"""

        return self._client

    def bootstrap(self) -> QdrantBootstrapResult:
        created: list[str] = []
        existing: list[str] = []
        created_indexes: list[str] = []
        for collection_name in COLLECTION_NAMES:
            if self._collection_exists(collection_name):
                existing.append(collection_name)
            else:
                self._client.create_collection(
                    collection_name=collection_name,
                    vectors_config=models.VectorParams(
                        size=self._embedding_dimensions,
                        distance=models.Distance.COSINE,
                    ),
                )
                created.append(collection_name)

            payload_schema = self._payload_schema(collection_name)
            for field_name, field_schema in PAYLOAD_INDEXES.items():
                if field_name in payload_schema:
                    continue
                self._client.create_payload_index(
                    collection_name=collection_name,
                    field_name=field_name,
                    field_schema=field_schema,
                )
                created_indexes.append(f"{collection_name}.{field_name}")

        return QdrantBootstrapResult(
            collections_created=tuple(created),
            collections_existing=tuple(existing),
            indexes_created=tuple(created_indexes),
        )

    def _collection_exists(self, collection_name: str) -> bool:
        return bool(self._client.collection_exists(collection_name))

    def _payload_schema(self, collection_name: str) -> dict[str, object]:
        collection = self._client.get_collection(collection_name)
        payload_schema = getattr(collection, "payload_schema", None)
        return dict(payload_schema or {})


def check_qdrant_health(url: str) -> ComponentCheck:
    """在无需向量维度的场景检查 Qdrant 服务是否可达。"""

    try:
        return QdrantBootstrapper(_local_qdrant_client(url), 1).health_check()
    except Exception:
        return ComponentCheck(
            component="qdrant",
            ready=False,
            code="QDRANT_UNAVAILABLE",
            message="无法创建 Qdrant 客户端，请检查服务地址和 Docker Desktop。",
        )


def _local_qdrant_client(url: str) -> QdrantClient:
    """本机 Qdrant 不应经过企业代理或系统 HTTP 代理。"""
    return QdrantClient(url=url, trust_env=False)
