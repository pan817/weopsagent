"""
三级存储记忆管理器 - 编排 Redis / ES / ChromaDB 三层级联检索和写入

读路径（级联降级）：
  L1 Redis Cache → L2 Elasticsearch → L3 ChromaDB
  命中即返回，未命中才降级到下一层

写路径（异步，不阻塞主流程）：
  ChromaDB 写入（同步，已有逻辑）→ 触发异步 Judge → Judge 通过 → ES + Redis 写入

对外接口：
  search(query, service, alert_type) -> str   # 返回格式化的知识库上下文
  store_async(knowledge_data)                 # 触发后台写入
"""
import atexit
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

from config.settings import settings

logger = logging.getLogger(__name__)

# 模块级写入线程池（store + hit-update 共用）
# max_workers=4：Judge 调用 LLM 耗时长，4 个并发足够
# atexit 确保进程退出时等待所有在途写入完成（覆盖 sys.exit / SIGTERM via lifespan）
_write_executor = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="mm-write",
)
atexit.register(_write_executor.shutdown, wait=True)


class MemoryManager:
    """
    三级存储记忆管理器

    - L1 Redis Cache：热点文档，毫秒级响应，按 service+alert_type 索引
    - L2 Elasticsearch：BM25 全文检索，百毫秒级，按服务过滤
    - L3 ChromaDB：语义向量检索，兜底层，处理关键词不匹配场景

    所有写操作异步执行（后台线程），不阻塞故障处理主流程。
    """

    def __init__(self):
        from memory.long_term import get_long_term_memory
        from memory.cache import RedisKnowledgeCache
        from memory.search import ESKnowledgeSearch
        from memory.judge import LLMJudge

        self._chroma = get_long_term_memory()
        self._cache = RedisKnowledgeCache()
        self._es = ESKnowledgeSearch()
        self._judge = LLMJudge()

        logger.info(
            f"[MemoryManager] 初始化完成 "
            f"redis={self._cache.is_available} "
            f"es={self._es.is_available}"
        )

    # ===== Read Path =====

    def search(
        self,
        query: str,
        service: Optional[str] = None,
        alert_type: Optional[str] = None,
    ) -> str:
        """
        三级级联检索，返回格式化的知识库上下文字符串

        L1 → L2 → L3，命中即返回。同时异步记录命中以更新热度。

        Args:
            query: 故障描述（语义检索 query）
            service: 服务名（用于 L1/L2 精确过滤）
            alert_type: 告警类型（用于 L1 热点索引）

        Returns:
            格式化字符串，可直接注入 Prompt
        """
        # L1: Redis 热点缓存
        if self._cache.is_available and service:
            l1_docs = self._cache.get_hot_docs(service, alert_type or "")
            if l1_docs:
                logger.info(f"[MemoryManager] L1 命中 {len(l1_docs)} 条 service={service}")
                self._async_update_hits(l1_docs, service, alert_type)
                return self._format_docs(l1_docs, source="L1-Redis")

        # L2: Elasticsearch BM25
        if self._es.is_available:
            l2_docs = self._es.search(query, service=service, top_k=3)
            if l2_docs:
                logger.info(f"[MemoryManager] L2 命中 {len(l2_docs)} 条")
                # 命中后异步更新 ES hit_count 并尝试晋升到 Redis
                self._async_update_hits(l2_docs, service, alert_type)
                return self._format_docs(l2_docs, source="L2-ES")

        # L3: ChromaDB 语义检索（兜底）
        logger.info(f"[MemoryManager] L3 降级到 ChromaDB service={service}")
        return self._chroma.format_context(
            query, top_k=6, score_threshold=settings.rag_score_threshold
        )

    # ===== Write Path =====

    def store_async(self, knowledge_data: Dict) -> None:
        """
        异步触发质量评估 + 多层写入（不阻塞调用方）

        knowledge_data 字段：
          doc_id, title, fault_description, root_cause, solution,
          service (optional), alert_type (optional),
          tags (list), effectiveness, category
        """
        doc_id_short = knowledge_data.get("doc_id", "")[:8]
        future = _write_executor.submit(self._store_pipeline, knowledge_data)
        future.add_done_callback(
            lambda f: logger.error(
                f"[MemoryManager] 后台存储异常 doc_id={doc_id_short}: {f.exception()}"
            ) if f.exception() else None
        )
        logger.debug(f"[MemoryManager] 已触发异步存储 doc_id={doc_id_short}")

    def _store_pipeline(self, data: Dict) -> None:
        """后台线程执行：Judge → ES → Redis"""
        doc_id = data.get("doc_id", "")
        title = data.get("title", "")

        try:
            # Step 1: LLM Judge 质量评估
            judge_result = self._judge.evaluate(
                title=title,
                fault_description=data.get("fault_description", ""),
                root_cause=data.get("root_cause", ""),
                solution=data.get("solution", ""),
            )

            if not judge_result.should_store:
                logger.info(
                    f"[MemoryManager] Judge 拒绝写入热路径 "
                    f"doc_id={doc_id[:8]} score={judge_result.score:.2f}"
                )
                return

            # 补充 Judge 输出字段
            data["judge_score"] = judge_result.score
            data["summary"] = judge_result.summary
            if judge_result.tags and not data.get("tags"):
                data["tags"] = judge_result.tags

            # Step 2: 写入 Elasticsearch
            self._es.index_doc(doc_id, data)

            # Step 3: 写入 Redis（仅存全量文档，待命中计数到达阈值后晋升热点）
            service = data.get("service", "")
            if self._cache.is_available and service:
                self._cache.put_doc(doc_id, self._doc_to_cache(data))
                # 新文档写入时，使对应 service 的热点缓存失效（保证一致性）
                self._cache.invalidate_service(service)

            logger.info(
                f"[MemoryManager] 多层写入完成 doc_id={doc_id[:8]} "
                f"score={judge_result.score:.2f} summary={judge_result.summary[:40]!r}"
            )
        except Exception as e:
            logger.error(f"[MemoryManager] 后台存储失败 doc_id={doc_id}: {e}")

    # ===== Internal Helpers =====

    def _async_update_hits(
        self,
        docs: List[Dict],
        service: Optional[str],
        alert_type: Optional[str],
    ) -> None:
        """异步更新命中计数（不阻塞检索路径）"""
        def _update():
            for doc in docs:
                doc_id = doc.get("doc_id", "")
                if not doc_id:
                    continue
                # 更新 ES hit_count
                self._es.increment_hit(doc_id)
                # 更新 Redis 命中计数并尝试晋升热点
                if self._cache.is_available and service:
                    self._cache.increment_hit(doc_id, service, alert_type or "")

        _write_executor.submit(_update)

    @staticmethod
    def _doc_to_cache(data: Dict) -> Dict:
        """将完整文档裁剪为适合 Redis 缓存的精简结构"""
        return {
            "doc_id": data.get("doc_id", ""),
            "title": data.get("title", ""),
            "service": data.get("service", ""),
            "alert_type": data.get("alert_type", ""),
            "fault_description": data.get("fault_description", "")[:300],
            "root_cause": data.get("root_cause", "")[:300],
            "solution": data.get("solution", "")[:500],
            "tags": data.get("tags", []),
            "judge_score": data.get("judge_score", 0.5),
            "summary": data.get("summary", ""),
            "effectiveness": data.get("effectiveness", "confirmed"),
        }

    @staticmethod
    def _format_docs(docs: List[Dict], source: str = "") -> str:
        """将文档列表格式化为 Prompt 注入字符串"""
        if not docs:
            return ""

        lines = [f"【知识库检索结果 - {source}】\n"]
        for i, doc in enumerate(docs, 1):
            score = doc.get("_es_score") or doc.get("judge_score", 0)
            summary = doc.get("summary", "")
            tags = ", ".join(doc.get("tags", []))

            section = [
                f"### 参考案例 {i}: {doc.get('title', '未知')}",
                f"**相关度**: {score:.2f}" if score else "",
                f"**摘要**: {summary}" if summary else "",
                f"**故障现象**: {doc.get('fault_description', '')[:300]}",
                f"**根因分析**: {doc.get('root_cause', '')[:300]}",
                f"**解决方案**: {doc.get('solution', '')[:400]}",
                f"**标签**: {tags}" if tags else "",
            ]
            lines.append("\n".join(s for s in section if s))

        return "\n\n".join(lines)[:3000]


# ===== 全局单例 =====
_manager: Optional["MemoryManager"] = None
_manager_lock = threading.Lock()


def get_memory_manager() -> MemoryManager:
    """获取全局 MemoryManager 单例（进程级懒加载）"""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = MemoryManager()
    return _manager
