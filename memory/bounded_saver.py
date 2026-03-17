"""
BoundedInMemorySaver - 有界 LRU 检查点存储

基于 InMemorySaver 扩展，提供：
- LRU 容量上限：超过 max_threads 时驱逐最久未使用的 thread
- TTL 驱逐：purge_expired() 清理超时 thread
- 公开 purge(thread_id) API：替代直接访问内部 storage 字典
- 线程安全：独立锁管理 LRU 元数据，不与父类 storage 产生锁竞争

所有子 Agent 和 FaultAgent 统一使用此类替换裸 InMemorySaver。
"""
import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Optional

from langgraph.checkpoint.memory import InMemorySaver

logger = logging.getLogger(__name__)


class BoundedInMemorySaver(InMemorySaver):
    """
    有界 LRU InMemorySaver

    在父类 InMemorySaver 基础上维护一个 OrderedDict 作为 LRU 元数据层：
    - key: thread_id
    - value: 最后写入时间戳（float）

    每次 put/aput 时更新 LRU；超过 max_threads 时自动驱逐最旧 thread。
    """

    def __init__(self, max_threads: int = 100, ttl_seconds: float = 7200.0):
        """
        Args:
            max_threads: 最多保留多少个 thread 的 checkpoint（LRU 驱逐）
            ttl_seconds: purge_expired() 使用的默认 TTL（秒）
        """
        super().__init__()
        self._max_threads = max_threads
        self._ttl_seconds = ttl_seconds
        # OrderedDict 维护 LRU 顺序：最旧在头，最新在尾
        self._lru: OrderedDict[str, float] = OrderedDict()
        self._bsaver_lock = threading.Lock()

    # ===== LRU 管理 =====

    def _record_write(self, thread_id: str) -> None:
        """记录写入，更新 LRU；超容量时驱逐最旧 thread（在 _bsaver_lock 外调用）"""
        evicted: list[str] = []
        with self._bsaver_lock:
            if thread_id in self._lru:
                self._lru.move_to_end(thread_id)
            else:
                self._lru[thread_id] = 0.0  # 先占位
            self._lru[thread_id] = time.time()
            # 超容量时驱逐
            while len(self._lru) > self._max_threads:
                oldest_id, _ = self._lru.popitem(last=False)
                evicted.append(oldest_id)

        for tid in evicted:
            self._delete_storage(tid)
            logger.debug(f"[BoundedSaver] LRU 驱逐 thread_id={tid}")

    def _delete_storage(self, thread_id: str) -> int:
        """删除 storage 中指定 thread 的所有 checkpoint 条目，返回删除数"""
        try:
            storage = getattr(self, "storage", None)
            if not isinstance(storage, dict):
                return 0
            keys = [k for k in list(storage.keys()) if k[0] == thread_id]
            for k in keys:
                storage.pop(k, None)
            return len(keys)
        except Exception as e:
            logger.debug(f"[BoundedSaver] 删除 storage 失败 thread_id={thread_id}: {e}")
            return 0

    # ===== 覆盖父类写入方法以钩入 LRU =====

    def put(self, config: Any, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
        thread_id = (config.get("configurable") or {}).get("thread_id", "")
        if thread_id:
            self._record_write(thread_id)
        return super().put(config, checkpoint, metadata, new_versions)

    async def aput(self, config: Any, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
        thread_id = (config.get("configurable") or {}).get("thread_id", "")
        if thread_id:
            self._record_write(thread_id)
        return await super().aput(config, checkpoint, metadata, new_versions)

    # ===== 公开清理 API =====

    def purge(self, thread_id: str) -> int:
        """
        清理指定 thread 的所有 checkpoints，从 LRU 和 storage 中移除。

        Args:
            thread_id: 要清理的 thread ID

        Returns:
            实际删除的 checkpoint 条目数
        """
        with self._bsaver_lock:
            self._lru.pop(thread_id, None)
        count = self._delete_storage(thread_id)
        if count:
            logger.debug(f"[BoundedSaver] purge thread_id={thread_id} 删除 {count} 条")
        return count

    def purge_expired(self, ttl_seconds: Optional[float] = None) -> int:
        """
        清理超过 TTL 未写入的所有 threads。

        Args:
            ttl_seconds: 覆盖默认 TTL，None 使用构造时的 ttl_seconds

        Returns:
            清理的 thread 数量
        """
        cutoff = time.time() - (ttl_seconds if ttl_seconds is not None else self._ttl_seconds)
        with self._bsaver_lock:
            expired = [tid for tid, ts in self._lru.items() if ts < cutoff]
            for tid in expired:
                self._lru.pop(tid, None)

        for tid in expired:
            self._delete_storage(tid)

        if expired:
            logger.debug(f"[BoundedSaver] purge_expired 清理 {len(expired)} 个过期 thread")
        return len(expired)

    def thread_count(self) -> int:
        """返回当前跟踪的 thread 数量"""
        with self._bsaver_lock:
            return len(self._lru)

    def stats(self) -> dict:
        """返回当前统计信息（供日志/监控使用）"""
        with self._bsaver_lock:
            count = len(self._lru)
            oldest_ts = min(self._lru.values(), default=0.0)
        try:
            storage_keys = len(getattr(self, "storage", {}) or {})
        except Exception:
            storage_keys = -1
        return {
            "tracked_threads": count,
            "max_threads": self._max_threads,
            "storage_keys": storage_keys,
            "oldest_write_age_s": round(time.time() - oldest_ts, 1) if oldest_ts else 0,
        }
