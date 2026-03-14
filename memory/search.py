"""
Elasticsearch 结构化检索层（L2）

文档结构：
  {
    "doc_id": "...",
    "title": "...",
    "service": "order-service",
    "alert_type": "db_connection",
    "fault_description": "...",
    "root_cause": "...",
    "solution": "...",
    "tags": ["mysql", "timeout"],
    "effectiveness": "confirmed",
    "judge_score": 0.92,
    "summary": "50字摘要",
    "hit_count": 5,
    "created_at": "2026-03-14T13:00:00Z"
  }

检索策略：
  multi_match BM25（fault_description + title + tags）
  + filter（service = ?, effectiveness = confirmed）
  + 按 _score + hit_count 加权排序

默认关闭（memory_es_enabled=false）。
"""
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from config.settings import settings

logger = logging.getLogger(__name__)

# Index mapping 定义
_INDEX_MAPPING = {
    "mappings": {
        "properties": {
            "doc_id": {"type": "keyword"},
            "title": {"type": "text", "analyzer": "standard"},
            "service": {"type": "keyword"},
            "alert_type": {"type": "keyword"},
            "fault_description": {"type": "text", "analyzer": "standard"},
            "root_cause": {"type": "text", "analyzer": "standard"},
            "solution": {"type": "text", "analyzer": "standard"},
            "tags": {"type": "keyword"},
            "effectiveness": {"type": "keyword"},
            "judge_score": {"type": "float"},
            "summary": {"type": "text"},
            "hit_count": {"type": "integer"},
            "created_at": {"type": "date"},
        }
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
    },
}


class ESKnowledgeSearch:
    """
    Elasticsearch 结构化检索（L2）

    提供 BM25 全文检索 + 关键词过滤，补充向量检索在精确关键词场景下的不足。
    hit_count 字段随每次被采用而递增，用于提升热门文档的排序权重。
    """

    def __init__(self):
        self._enabled = settings.memory_es_enabled
        self._index = settings.memory_es_index
        self._score_threshold = settings.memory_es_score_threshold
        self._client = None
        if self._enabled:
            self._init_client()

    def _init_client(self) -> None:
        try:
            from elasticsearch import Elasticsearch
            kwargs: Dict = {"hosts": [settings.memory_es_url]}
            if settings.memory_es_user:
                kwargs["basic_auth"] = (
                    settings.memory_es_user,
                    settings.memory_es_password or "",
                )
            self._client = Elasticsearch(**kwargs, request_timeout=10)
            if self._client.ping():
                self._ensure_index()
                logger.info(f"[ESSearch] 已连接 {settings.memory_es_url}")
            else:
                logger.warning("[ESSearch] Elasticsearch ping 失败，L2 检索不可用")
                self._client = None
        except Exception as e:
            logger.warning(f"[ESSearch] 连接失败，L2 检索不可用: {e}")
            self._client = None

    def _ensure_index(self) -> None:
        """确保索引和 mapping 存在"""
        try:
            if not self._client.indices.exists(index=self._index):
                self._client.indices.create(index=self._index, body=_INDEX_MAPPING)
                logger.info(f"[ESSearch] 已创建索引 {self._index}")
        except Exception as e:
            logger.warning(f"[ESSearch] 索引初始化失败: {e}")

    @property
    def is_available(self) -> bool:
        return self._enabled and self._client is not None

    # ===== Read =====

    def search(
        self,
        query: str,
        service: Optional[str] = None,
        top_k: int = 3,
    ) -> List[Dict]:
        """
        BM25 全文检索

        Args:
            query: 故障描述查询文本
            service: 限定服务名（可选，显著提升精准度）
            top_k: 最多返回条数

        Returns:
            按相关度排序的文档 dict 列表，未命中或低于阈值返回空列表
        """
        if not self.is_available:
            return []

        try:
            # 构建查询
            must_clauses = [
                {
                    "multi_match": {
                        "query": query,
                        "fields": ["fault_description^2", "title^1.5", "root_cause", "tags"],
                        "type": "best_fields",
                        "fuzziness": "AUTO",
                    }
                }
            ]
            filter_clauses = [
                {"term": {"effectiveness": "confirmed"}}
            ]
            if service and service.lower() != "unknown":
                filter_clauses.append({"term": {"service": service}})

            body = {
                "query": {
                    "function_score": {
                        "query": {
                            "bool": {
                                "must": must_clauses,
                                "filter": filter_clauses,
                            }
                        },
                        "field_value_factor": {
                            "field": "hit_count",
                            "factor": 0.1,
                            "modifier": "log1p",
                            "missing": 0,
                        },
                        "boost_mode": "sum",
                    }
                },
                "size": top_k,
                "_source": [
                    "doc_id", "title", "service", "alert_type",
                    "fault_description", "root_cause", "solution",
                    "tags", "judge_score", "summary", "hit_count",
                ],
            }

            resp = self._client.search(index=self._index, body=body)
            hits = resp.get("hits", {}).get("hits", [])

            docs = []
            max_score = resp.get("hits", {}).get("max_score") or 1.0
            for hit in hits:
                # 归一化分数
                normalized = (hit.get("_score", 0) / max_score) if max_score > 0 else 0
                if normalized < self._score_threshold:
                    continue
                doc = hit["_source"]
                doc["_es_score"] = round(normalized, 4)
                docs.append(doc)

            logger.info(
                f"[ESSearch] 检索完成 query={query[:30]!r} "
                f"service={service} hits={len(docs)}/{len(hits)}"
            )
            return docs

        except Exception as e:
            logger.warning(f"[ESSearch] 检索失败: {e}")
            return []

    # ===== Write =====

    def index_doc(self, doc_id: str, data: Dict) -> None:
        """
        将故障处理经验写入 ES 索引

        Args:
            doc_id: 文档唯一 ID（与 ChromaDB 保持一致）
            data: 文档字段 dict
        """
        if not self.is_available:
            return
        try:
            doc = {
                "doc_id": doc_id,
                "title": data.get("title", ""),
                "service": data.get("service", "unknown"),
                "alert_type": data.get("alert_type", ""),
                "fault_description": data.get("fault_description", ""),
                "root_cause": data.get("root_cause", ""),
                "solution": data.get("solution", ""),
                "tags": data.get("tags", []),
                "effectiveness": data.get("effectiveness", "confirmed"),
                "judge_score": data.get("judge_score", 0.5),
                "summary": data.get("summary", ""),
                "hit_count": 0,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            self._client.index(
                index=self._index,
                id=doc_id,
                body=doc,
            )
            logger.info(f"[ESSearch] 已写入文档 doc_id={doc_id} title={data.get('title')!r}")
        except Exception as e:
            logger.warning(f"[ESSearch] 写入失败 doc_id={doc_id}: {e}")

    def increment_hit(self, doc_id: str) -> None:
        """文档被采用时累加 hit_count"""
        if not self.is_available:
            return
        try:
            self._client.update(
                index=self._index,
                id=doc_id,
                body={"script": {"source": "ctx._source.hit_count += 1", "lang": "painless"}},
                ignore=[404],
            )
        except Exception as e:
            logger.debug(f"[ESSearch] 更新 hit_count 失败 doc_id={doc_id}: {e}")
