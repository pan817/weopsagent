"""
数据库监控工具 - 检查数据库连接、慢 SQL、连接池状态等

使用 LangChain 1.2.x @tool 装饰器实现，不再继承 BaseTool 类。
"""
import json
import logging
import time
from typing import Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text

from config.settings import settings
from .base import format_tool_result, with_retry

logger = logging.getLogger(__name__)


class DBMonitorInput(BaseModel):
    """数据库监控工具输入参数"""
    host: str = Field(default=None, description="数据库服务器地址")
    port: int = Field(default=None, description="数据库端口")
    username: str = Field(default=None, description="数据库用户名")
    password: str = Field(default=None, description="数据库密码")
    database: str = Field(default=None, description="数据库名称")
    slow_query_threshold_ms: int = Field(
        default=1000,
        description="慢查询阈值（毫秒），超过此值视为慢查询"
    )
    top_n_slow_queries: int = Field(default=10, description="返回前 N 条慢查询")


@tool("monitor_database", args_schema=DBMonitorInput)
def monitor_database(
    host: str = None,
    port: int = None,
    username: str = None,
    password: str = None,
    database: str = None,
    slow_query_threshold_ms: int = 1000,
    top_n_slow_queries: int = 10,
) -> str:
    """监控数据库运行状态，包括连接池状态、慢查询、锁等待等。可以快速发现数据库性能瓶颈和连接问题。"""
    start_time = time.time()
    target_host = host or settings.monitor_db_host
    target_port = port or settings.monitor_db_port
    target_user = username or settings.monitor_db_user
    target_pass = password or settings.monitor_db_password
    target_db = database or settings.monitor_db_name

    try:
        result = _collect_db_info(
            host=target_host,
            port=target_port,
            username=target_user,
            password=target_pass,
            database=target_db,
            slow_query_threshold_ms=slow_query_threshold_ms,
            top_n_slow_queries=top_n_slow_queries,
        )
        elapsed = time.time() - start_time
        return json.dumps(
            format_tool_result("monitor_database", True, result, elapsed=elapsed),
            ensure_ascii=False,
        )
    except Exception as e:
        elapsed = time.time() - start_time
        logger.error(f"[DBMonitor] 监控失败: {e}")
        return json.dumps(
            format_tool_result("monitor_database", False, error=str(e), elapsed=elapsed),
            ensure_ascii=False,
        )


@with_retry(exceptions=(Exception,))
def _collect_db_info(
    host: str,
    port: int,
    username: str,
    password: Optional[str],
    database: str,
    slow_query_threshold_ms: int,
    top_n_slow_queries: int,
) -> dict:
    """连接数据库并收集信息（MySQL，带重试）"""
    db_url = (
        f"mysql+pymysql://{username}:{password or ''}@{host}:{port}/{database}"
        f"?connect_timeout=10&charset=utf8mb4"
    )
    engine = create_engine(db_url, pool_pre_ping=True, pool_size=1, max_overflow=0)

    result = {"host": host, "port": port, "database": database, "connected": False}

    with engine.connect() as conn:
        result["connected"] = True

        row = conn.execute(text("SELECT VERSION()")).fetchone()
        result["db_version"] = row[0] if row else "unknown"

        rows = conn.execute(text(
            "SELECT COUNT(*) as total, "
            "SUM(CASE WHEN command != 'Sleep' THEN 1 ELSE 0 END) as active "
            "FROM information_schema.PROCESSLIST"
        )).fetchone()
        if rows:
            result["total_connections"] = rows[0]
            result["active_connections"] = rows[1]

        row = conn.execute(text("SHOW VARIABLES LIKE 'max_connections'")).fetchone()
        result["max_connections"] = int(row[1]) if row else None

        slow_queries = []
        try:
            slow_q_rows = conn.execute(text(
                "SELECT * FROM information_schema.PROCESSLIST "
                "WHERE command != 'Sleep' "
                f"AND time > {slow_query_threshold_ms // 1000} "
                "ORDER BY time DESC "
                f"LIMIT {top_n_slow_queries}"
            )).fetchall()
            for row in slow_q_rows:
                slow_queries.append({
                    "id": row[0],
                    "user": row[1],
                    "db": row[3],
                    "command": row[4],
                    "time_seconds": row[5],
                    "state": row[6],
                    "info": str(row[7])[:300] if row[7] else None,
                })
        except Exception as e:
            logger.debug(f"[DBMonitor] 慢查询获取失败: {e}")

        result["slow_queries"] = slow_queries

        lock_waits = []
        try:
            lock_rows = conn.execute(text(
                "SELECT r.trx_id waiting_trx_id, r.trx_mysql_thread_id waiting_thread, "
                "r.trx_query waiting_query, b.trx_id blocking_trx_id, "
                "b.trx_mysql_thread_id blocking_thread "
                "FROM information_schema.innodb_lock_waits w "
                "INNER JOIN information_schema.innodb_trx b ON b.trx_id = w.blocking_trx_id "
                "INNER JOIN information_schema.innodb_trx r ON r.trx_id = w.requesting_trx_id "
                "LIMIT 10"
            )).fetchall()
            for row in lock_rows:
                lock_waits.append({
                    "waiting_thread": row[1],
                    "waiting_query": str(row[2])[:200] if row[2] else None,
                    "blocking_thread": row[4],
                })
        except Exception:
            pass

        result["lock_waits"] = lock_waits

        try:
            status_rows = conn.execute(text(
                "SHOW GLOBAL STATUS WHERE variable_name IN ("
                "'Innodb_buffer_pool_reads', 'Innodb_buffer_pool_read_requests', "
                "'Queries', 'Slow_queries', 'Threads_running', 'Threads_connected')"
            )).fetchall()
            result["global_status"] = {row[0]: row[1] for row in status_rows}
        except Exception:
            pass

    engine.dispose()
    return result
