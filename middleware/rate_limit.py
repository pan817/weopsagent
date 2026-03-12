"""
限流中间件 - 对 LLM 调用和工具调用进行速率限制

基于 LangChain 1.2.x 的 AgentMiddleware 机制实现，
通过 before_model 和 wrap_tool_call 两个 hook 分别限制 LLM 和工具的调用频率。

支持两种限流策略：
1. wait（默认）：超限时自动等待，直到令牌可用
2. reject：超限时直接拒绝，抛出 RateLimitError

支持三种限流维度：
- 全局 LLM 调用限流：限制每个时间窗口内的 LLM 调用次数
- 全局工具调用限流：限制每个时间窗口内所有工具的总调用次数
- 单工具限流：为特定工具设置独立的调用频率限制

使用方式：
    middleware = RateLimitMiddleware(
        model_rpm=20,                          # LLM 每分钟最多 20 次
        tool_rpm=60,                           # 工具每分钟总计最多 60 次
        per_tool_rpm={"monitor_process": 10},  # 单工具独立限流
        strategy="wait",                       # 超限时等待（也可设为 "reject"）
    )
    agent = create_agent(..., middleware=[middleware])
"""
import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from langchain.agents.middleware.types import AgentMiddleware

logger = logging.getLogger(__name__)


class RateLimitError(Exception):
    """限流拒绝异常，strategy="reject" 时超限会抛出此异常"""
    pass


@dataclass
class _TokenBucket:
    """
    令牌桶限流器

    以固定速率向桶中添加令牌，每次请求消耗一个令牌。
    桶满时多余令牌被丢弃，桶空时请求被阻塞或拒绝。

    Attributes:
        capacity: 桶容量（突发上限）
        refill_rate: 每秒补充的令牌数
        tokens: 当前可用令牌数
        last_refill: 上次补充令牌的时间戳
    """
    capacity: float
    refill_rate: float
    tokens: float = field(init=False)
    last_refill: float = field(init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self):
        self.tokens = self.capacity
        self.last_refill = time.monotonic()

    def _refill(self):
        """补充令牌（根据时间流逝计算）"""
        now = time.monotonic()
        elapsed = now - self.last_refill
        added = elapsed * self.refill_rate
        self.tokens = min(self.capacity, self.tokens + added)
        self.last_refill = now

    def try_acquire(self) -> bool:
        """尝试获取一个令牌，成功返回 True"""
        with self._lock:
            self._refill()
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
            return False

    def wait_and_acquire(self, timeout: float = 60.0) -> bool:
        """
        等待并获取一个令牌

        Args:
            timeout: 最大等待时间（秒），超时返回 False

        Returns:
            True 表示成功获取，False 表示超时
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                self._refill()
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return True
                # 计算需要等待的时间
                wait_time = (1.0 - self.tokens) / self.refill_rate
            # 在锁外等待，避免阻塞其他线程
            sleep_time = min(wait_time, deadline - time.monotonic(), 1.0)
            if sleep_time > 0:
                time.sleep(sleep_time)
        return False

    def time_until_available(self) -> float:
        """返回下一个令牌可用的预计等待秒数"""
        with self._lock:
            self._refill()
            if self.tokens >= 1.0:
                return 0.0
            return (1.0 - self.tokens) / self.refill_rate


class RateLimitMiddleware(AgentMiddleware):
    """
    限流 AgentMiddleware（LangChain 1.2.x）

    通过 before_model 和 wrap_tool_call 两个 hook 分别对 LLM 调用
    和工具调用进行速率限制，防止短时间内过多请求导致 API 配额耗尽或
    下游服务过载。

    内部使用令牌桶算法，支持突发流量同时保证长期平均速率不超限。
    """

    def __init__(
        self,
        model_rpm: Optional[int] = None,
        tool_rpm: Optional[int] = None,
        per_tool_rpm: Optional[Dict[str, int]] = None,
        strategy: str = "wait",
        wait_timeout: float = 60.0,
    ):
        """
        Args:
            model_rpm: LLM 每分钟最大调用次数（None 表示不限流）
            tool_rpm: 工具每分钟总调用次数上限（None 表示不限流）
            per_tool_rpm: 单工具独立限流，如 {"monitor_process": 10, "analyze_logs": 5}
            strategy: 超限策略，"wait"=等待令牌恢复，"reject"=直接拒绝
            wait_timeout: strategy="wait" 时的最大等待秒数
        """
        self.strategy = strategy
        self.wait_timeout = wait_timeout

        # 构建令牌桶
        self._model_bucket: Optional[_TokenBucket] = None
        if model_rpm and model_rpm > 0:
            self._model_bucket = _TokenBucket(
                capacity=float(model_rpm),
                refill_rate=model_rpm / 60.0,
            )

        self._tool_bucket: Optional[_TokenBucket] = None
        if tool_rpm and tool_rpm > 0:
            self._tool_bucket = _TokenBucket(
                capacity=float(tool_rpm),
                refill_rate=tool_rpm / 60.0,
            )

        self._per_tool_buckets: Dict[str, _TokenBucket] = {}
        for tool_name, rpm in (per_tool_rpm or {}).items():
            if rpm and rpm > 0:
                self._per_tool_buckets[tool_name] = _TokenBucket(
                    capacity=float(rpm),
                    refill_rate=rpm / 60.0,
                )

        # 统计计数器
        self._model_total: int = 0
        self._model_waited: int = 0
        self._model_rejected: int = 0
        self._tool_total: int = 0
        self._tool_waited: int = 0
        self._tool_rejected: int = 0
        self._stats_lock = threading.Lock()

        limits = []
        if model_rpm:
            limits.append(f"model={model_rpm}/min")
        if tool_rpm:
            limits.append(f"tool_global={tool_rpm}/min")
        for tn, rpm in (per_tool_rpm or {}).items():
            limits.append(f"{tn}={rpm}/min")

        logger.info(
            f"[RateLimit] 初始化完成 strategy={strategy} "
            f"limits=[{', '.join(limits) or '无限制'}]"
        )

    # ===== Agent 生命周期 =====

    def before_agent(self, state: Any, runtime: Any) -> Any:
        """Agent 循环开始时重置统计计数器"""
        with self._stats_lock:
            self._model_total = 0
            self._model_waited = 0
            self._model_rejected = 0
            self._tool_total = 0
            self._tool_waited = 0
            self._tool_rejected = 0
        return None

    def after_agent(self, state: Any, runtime: Any) -> Any:
        """Agent 循环结束时输出限流统计"""
        with self._stats_lock:
            if self._model_waited > 0 or self._tool_waited > 0 or \
               self._model_rejected > 0 or self._tool_rejected > 0:
                logger.info(
                    f"[RateLimit] 统计: "
                    f"model(total={self._model_total}, waited={self._model_waited}, rejected={self._model_rejected}) "
                    f"tool(total={self._tool_total}, waited={self._tool_waited}, rejected={self._tool_rejected})"
                )
        return None

    # ===== LLM 调用限流 =====

    def before_model(self, state: Any, runtime: Any) -> Any:
        """LLM 调用前检查限流"""
        if self._model_bucket is None:
            return None

        with self._stats_lock:
            self._model_total += 1

        if self._model_bucket.try_acquire():
            return None

        # 令牌不足
        if self.strategy == "reject":
            with self._stats_lock:
                self._model_rejected += 1
            wait_sec = self._model_bucket.time_until_available()
            logger.warning(
                f"[RateLimit] LLM 调用被限流拒绝，预计 {wait_sec:.1f}s 后恢复"
            )
            raise RateLimitError(
                f"LLM 调用超过速率限制，请在 {wait_sec:.1f} 秒后重试"
            )

        # strategy == "wait"
        wait_sec = self._model_bucket.time_until_available()
        logger.info(f"[RateLimit] LLM 调用限流等待 {wait_sec:.1f}s")
        with self._stats_lock:
            self._model_waited += 1

        if not self._model_bucket.wait_and_acquire(timeout=self.wait_timeout):
            with self._stats_lock:
                self._model_rejected += 1
            raise RateLimitError(
                f"LLM 调用限流等待超时（{self.wait_timeout}s）"
            )

        return None

    # ===== 工具调用限流 =====

    def wrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        """
        工具调用限流

        依次检查：
        1. 全局工具限流（tool_rpm）
        2. 单工具限流（per_tool_rpm）
        通过后再执行 handler(request)。
        """
        tool_call = getattr(request, "tool_call", {}) or {}
        tool_name = tool_call.get("name", "unknown")

        with self._stats_lock:
            self._tool_total += 1

        # 1. 全局工具限流
        if self._tool_bucket is not None:
            self._check_bucket(
                self._tool_bucket,
                label=f"工具全局({tool_name})",
            )

        # 2. 单工具限流
        per_tool_bucket = self._per_tool_buckets.get(tool_name)
        if per_tool_bucket is not None:
            self._check_bucket(
                per_tool_bucket,
                label=f"工具({tool_name})",
            )

        return handler(request)

    def _check_bucket(self, bucket: _TokenBucket, label: str) -> None:
        """检查令牌桶，根据策略等待或拒绝"""
        if bucket.try_acquire():
            return

        if self.strategy == "reject":
            with self._stats_lock:
                self._tool_rejected += 1
            wait_sec = bucket.time_until_available()
            logger.warning(
                f"[RateLimit] {label} 调用被限流拒绝，预计 {wait_sec:.1f}s 后恢复"
            )
            raise RateLimitError(
                f"{label} 调用超过速率限制，请在 {wait_sec:.1f} 秒后重试"
            )

        # strategy == "wait"
        wait_sec = bucket.time_until_available()
        logger.info(f"[RateLimit] {label} 调用限流等待 {wait_sec:.1f}s")
        with self._stats_lock:
            self._tool_waited += 1

        if not bucket.wait_and_acquire(timeout=self.wait_timeout):
            with self._stats_lock:
                self._tool_rejected += 1
            raise RateLimitError(
                f"{label} 调用限流等待超时（{self.wait_timeout}s）"
            )

    # ===== 运行时管理 =====

    def get_stats(self) -> Dict[str, Any]:
        """获取当前限流统计信息"""
        with self._stats_lock:
            return {
                "model": {
                    "total": self._model_total,
                    "waited": self._model_waited,
                    "rejected": self._model_rejected,
                },
                "tool": {
                    "total": self._tool_total,
                    "waited": self._tool_waited,
                    "rejected": self._tool_rejected,
                },
            }
