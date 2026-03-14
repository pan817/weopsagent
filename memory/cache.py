"""
Redis 热点知识缓存（L1）

Key 设计：
  weops:kb:{doc_id}              → JSON 文档全量数据（包含 title/root_cause/solution 等）
  weops:hits:{doc_id}            → 命中计数（整数）
  weops:hot:{service}:{alert}    → 热点文档 ID 列表（sorted set，score=hit_count，TTL 跟随 Redis）

读路径：
  1. 按 service+alert_type 查 sorted set 获取热点 doc_id 列表
  2. 批量 GET weops:kb:{doc_id} 返回文档

写路径（由 MemoryManager 异步触发）：
  1. PUT weops:kb:{doc_id} = 文档 JSON（TTL = memory_redis_ttl）
  2. INCR weops:hits:{doc_id}
  3. 若 hit_count >= memory_redis_hit_threshold，加入 hot sorted set

默认关闭（memory_redis_enabled=false）。
"""
import json
import logging
from typing import Dict, List, Optional

from config.settings import settings

logger = logging.getLogger(__name__)

# Key 前缀常量
_PREFIX_DOC = "weops:kb:"
_PREFIX_HIT = "weops:hits:"
_PREFIX_HOT = "weops:hot:"
_HOT_SET_MAX = 20  # 每个 service+alert 最多保留的热点文档数


class RedisKnowledgeCache:
    """
    Redis 热点知识缓存（L1）

    提供基于命中频率的知识文档热点晋升机制：
    - 文档首次写入时只存储全量数据（weops:kb:）
    - 每次被检索命中时计数 +1（weops:hits:）
    - 命中次数 >= 阈值时晋升为热点（weops:hot: sorted set）
    - 热点查询通过 service+alert_type 索引返回最热文档列表
    """

    def __init__(self):
        self._enabled = settings.memory_redis_enabled
        self._ttl = settings.memory_redis_ttl
        self._hit_threshold = settings.memory_redis_hit_threshold
        self._client = None
        if self._enabled:
            self._init_client()

    def _init_client(self) -> None:
        try:
            import redis
            self._client = redis.from_url(
                settings.memory_redis_url,
                decode_responses=True,
                socket_connect_timeout=3,
                socket_timeout=3,
            )
            self._client.ping()
            logger.info(f"[RedisCache] 已连接 {settings.memory_redis_url}")
        except Exception as e:
            logger.warning(f"[RedisCache] 连接失败，L1 缓存不可用: {e}")
            self._client = None

    @property
    def is_available(self) -> bool:
        return self._enabled and self._client is not None

    # ===== Read =====

    def get_hot_docs(self, service: str, alert_type: str = "") -> List[Dict]:
        """
        根据 service+alert_type 获取热点文档列表

        Returns:
            按命中次数降序排列的文档 dict 列表，未命中返回空列表
        """
        if not self.is_available:
            return []

        hot_key = _hot_key(service, alert_type)
        try:
            # 从 sorted set 取最热 top N 的 doc_id（score 降序）
            doc_ids = self._client.zrevrange(hot_key, 0, 4)
            if not doc_ids:
                return []

            pipe = self._client.pipeline()
            for doc_id in doc_ids:
                pipe.get(_PREFIX_DOC + doc_id)
            raw_list = pipe.execute()

            docs = []
            for raw in raw_list:
                if raw:
                    try:
                        docs.append(json.loads(raw))
                    except json.JSONDecodeError:
                        pass
            logger.debug(f"[RedisCache] L1 命中 {len(docs)} 条文档 service={service} alert={alert_type}")
            return docs
        except Exception as e:
            logger.warning(f"[RedisCache] 查询热点失败: {e}")
            return []

    # ===== Write =====

    def put_doc(self, doc_id: str, doc_data: Dict) -> None:
        """
        存储文档全量数据到 Redis

        仅存储数据，不自动晋升为热点（需要 increment_hit 到达阈值后才晋升）。
        """
        if not self.is_available:
            return
        try:
            self._client.setex(
                _PREFIX_DOC + doc_id,
                self._ttl,
                json.dumps(doc_data, ensure_ascii=False),
            )
            logger.debug(f"[RedisCache] 已存储文档 doc_id={doc_id}")
        except Exception as e:
            logger.warning(f"[RedisCache] 存储文档失败: {e}")

    def increment_hit(
        self,
        doc_id: str,
        service: str,
        alert_type: str = "",
    ) -> int:
        """
        增加文档命中计数，达到阈值时自动晋升为热点

        Returns:
            命中后的新计数
        """
        if not self.is_available:
            return 0
        try:
            hit_count = self._client.incr(_PREFIX_HIT + doc_id)
            # 为 hit_count key 设置 TTL，避免无限增长
            self._client.expire(_PREFIX_HIT + doc_id, self._ttl)

            if hit_count >= self._hit_threshold:
                self._promote_to_hot(doc_id, service, alert_type, hit_count)

            return hit_count
        except Exception as e:
            logger.warning(f"[RedisCache] 更新命中计数失败: {e}")
            return 0

    def _promote_to_hot(
        self,
        doc_id: str,
        service: str,
        alert_type: str,
        hit_count: int,
    ) -> None:
        """将文档加入热点 sorted set，并维护最大容量"""
        hot_key = _hot_key(service, alert_type)
        try:
            pipe = self._client.pipeline()
            # zadd 以 hit_count 为 score，相同 doc_id 会更新 score
            pipe.zadd(hot_key, {doc_id: hit_count})
            # 保留 score 最高的 _HOT_SET_MAX 个，删除多余的低分文档
            pipe.zremrangebyrank(hot_key, 0, -(
                _HOT_SET_MAX + 1))
            # hot set 本身设置 TTL（与文档 TTL 一致）
            pipe.expire(hot_key, self._ttl)
            pipe.execute()
            logger.info(
                f"[RedisCache] 文档晋升热点 doc_id={doc_id} "
                f"service={service} alert={alert_type} hits={hit_count}"
            )
        except Exception as e:
            logger.warning(f"[RedisCache] 晋升热点失败: {e}")

    def invalidate_service(self, service: str) -> None:
        """使指定 service 的所有热点缓存失效（写入新知识后调用）"""
        if not self.is_available:
            return
        try:
            pattern = f"{_PREFIX_HOT}{service}:*"
            keys = list(self._client.scan_iter(pattern, count=50))
            if keys:
                self._client.delete(*keys)
                logger.info(f"[RedisCache] 已失效 {len(keys)} 个热点 key: service={service}")
        except Exception as e:
            logger.warning(f"[RedisCache] 失效缓存失败: {e}")


def _hot_key(service: str, alert_type: str) -> str:
    """生成热点 sorted set 的 Redis Key"""
    safe_svc = service.replace(":", "_").replace(" ", "_")
    safe_alert = (alert_type or "default").replace(":", "_").replace(" ", "_")
    return f"{_PREFIX_HOT}{safe_svc}:{safe_alert}"
