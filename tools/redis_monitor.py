"""
Redis 监控工具 - 检查 Redis 连接状态、内存使用、Key 统计等
"""
import json
import logging
import time
from typing import Any, Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from config.settings import settings
from .base import format_tool_result, safe_execute, with_retry

logger = logging.getLogger(__name__)


class RedisMonitorInput(BaseModel):
    """Redis 监控工具输入参数"""
    host: str = Field(default=None, description="Redis 服务器地址，默认使用配置值")
    port: int = Field(default=None, description="Redis 端口，默认使用配置值")
    password: str = Field(default=None, description="Redis 密码")
    check_keys_pattern: str = Field(default=None, description="要统计的 Key 匹配模式，如 'session:*'")


class RedisMonitorTool(BaseTool):
    """Redis 状态监控工具

    连接 Redis 服务器，收集运行状态信息，包括：
    - 连接是否正常
    - 内存使用情况
    - 连接客户端数量
    - 命令执行统计
    - 慢查询日志
    - Key 数量统计
    """
    name: str = "monitor_redis"
    description: str = (
        "监控 Redis 服务器的运行状态，包括内存使用、连接数、"
        "命令统计、慢查询等。可以快速判断 Redis 是否存在性能问题。"
        "输入参数: host（Redis 地址，可选）, port（端口，可选）, "
        "check_keys_pattern（Key 统计模式，可选）"
    )
    args_schema: Type[BaseModel] = RedisMonitorInput

    def _run(
        self,
        host: str = None,
        port: int = None,
        password: str = None,
        check_keys_pattern: str = None,
    ) -> str:
        """执行 Redis 监控"""
        start_time = time.time()
        target_host = host or settings.monitor_redis_host
        target_port = port or settings.monitor_redis_port
        target_password = password or settings.monitor_redis_password

        try:
            result = self._collect_redis_info(
                host=target_host,
                port=target_port,
                password=target_password,
                keys_pattern=check_keys_pattern,
            )
            elapsed = time.time() - start_time
            return json.dumps(
                format_tool_result("monitor_redis", True, result, elapsed=elapsed),
                ensure_ascii=False,
            )
        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(f"[RedisMonitor] 监控失败 {target_host}:{target_port}: {e}")
            return json.dumps(
                format_tool_result("monitor_redis", False, error=str(e), elapsed=elapsed),
                ensure_ascii=False,
            )

    @with_retry(exceptions=(ConnectionError, TimeoutError))
    def _collect_redis_info(
        self,
        host: str,
        port: int,
        password: Optional[str],
        keys_pattern: Optional[str],
    ) -> dict:
        """连接 Redis 并收集状态信息"""
        import redis

        r = redis.Redis(
            host=host,
            port=port,
            password=password,
            db=settings.monitor_redis_db,
            socket_connect_timeout=5,
            socket_timeout=5,
            decode_responses=True,
        )

        # Ping 检测连接
        r.ping()

        # 获取 INFO 统计
        info = r.info()

        # 提取关键指标
        memory_info = {
            "used_memory_human": info.get("used_memory_human"),
            "used_memory_peak_human": info.get("used_memory_peak_human"),
            "maxmemory_human": info.get("maxmemory_human", "no limit"),
            "mem_fragmentation_ratio": info.get("mem_fragmentation_ratio"),
        }

        connection_info = {
            "connected_clients": info.get("connected_clients"),
            "blocked_clients": info.get("blocked_clients"),
            "rejected_connections": info.get("rejected_connections"),
        }

        stats_info = {
            "total_commands_processed": info.get("total_commands_processed"),
            "instantaneous_ops_per_sec": info.get("instantaneous_ops_per_sec"),
            "total_net_input_bytes": info.get("total_net_input_bytes"),
            "total_net_output_bytes": info.get("total_net_output_bytes"),
            "keyspace_hits": info.get("keyspace_hits"),
            "keyspace_misses": info.get("keyspace_misses"),
            "evicted_keys": info.get("evicted_keys"),
            "expired_keys": info.get("expired_keys"),
        }

        # 慢查询日志（最近 10 条）
        slow_log = []
        try:
            raw_slow_log = r.slowlog_get(10)
            for entry in raw_slow_log:
                slow_log.append({
                    "id": entry.get("id"),
                    "duration_us": entry.get("duration"),
                    "command": " ".join(str(a) for a in entry.get("command", []))[:200],
                })
        except Exception:
            pass

        # Key 数量统计
        keyspace_info = info.get("keyspace", {})
        total_keys = sum(
            int(v.get("keys", 0)) if isinstance(v, dict) else 0
            for v in keyspace_info.values()
        ) if keyspace_info else r.dbsize()

        # 特定 Pattern 的 Key 数量
        pattern_key_count = None
        if keys_pattern:
            try:
                pattern_key_count = len(list(r.scan_iter(keys_pattern, count=100)))
            except Exception:
                pass

        r.close()

        return {
            "host": host,
            "port": port,
            "connected": True,
            "server_version": info.get("redis_version"),
            "uptime_days": info.get("uptime_in_days"),
            "memory": memory_info,
            "connections": connection_info,
            "stats": stats_info,
            "total_keys": total_keys,
            "slow_log": slow_log[:5],  # 最多返回 5 条
            "pattern_key_count": pattern_key_count,
            "replication_role": info.get("role"),
        }

    async def _arun(self, *args, **kwargs) -> str:
        return self._run(*args, **kwargs)
