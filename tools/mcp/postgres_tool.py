"""
PostgreSQL MCP 工具 - 通过 psycopg2 直接查询 PostgreSQL 数据库

提供 SQL 查询、慢查询诊断、表结构信息三个工具。
使用参数化查询防止 SQL 注入，只读连接确保安全。

典型用法：
- 执行诊断 SQL（连接数、锁等待、表大小）
- 查看当前慢查询和活跃会话
- 获取表结构和索引信息
"""
import json
import logging
import time
from typing import Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from config.settings import settings
from tools.base import format_tool_result

logger = logging.getLogger(__name__)


def _get_connection():
    """获取 PostgreSQL 只读连接"""
    import psycopg2
    import psycopg2.extras
    conn = psycopg2.connect(
        dsn=settings.mcp_postgres_dsn,
        connect_timeout=10,
        options="-c statement_timeout=30000 -c default_transaction_read_only=on",
    )
    conn.autocommit = True
    return conn


def _execute_query(sql: str, params: tuple = None, max_rows: int = 100) -> dict:
    """执行查询并返回结果"""
    conn = None
    try:
        conn = _get_connection()
        import psycopg2.extras
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            if cur.description:
                columns = [desc[0] for desc in cur.description]
                rows = cur.fetchmany(max_rows)
                return {
                    "columns": columns,
                    "rows": [dict(row) for row in rows],
                    "row_count": cur.rowcount,
                    "truncated": cur.rowcount > max_rows,
                }
            return {"affected_rows": cur.rowcount}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    finally:
        if conn:
            conn.close()


class PgQueryInput(BaseModel):
    """PostgreSQL 查询输入"""
    sql: str = Field(description="SQL 查询语句（只读，不支持 INSERT/UPDATE/DELETE）。如 'SELECT count(*) FROM pg_stat_activity'")
    max_rows: int = Field(default=50, description="最大返回行数（1-500）")


class PgSlowQueriesInput(BaseModel):
    """PostgreSQL 慢查询诊断输入"""
    min_duration_ms: int = Field(default=1000, description="最小执行时间（毫秒），筛选慢于此值的查询")
    top_n: int = Field(default=10, description="返回前 N 条慢查询")


class PgTableInfoInput(BaseModel):
    """PostgreSQL 表信息查询输入"""
    table_name: str = Field(description="表名，如 'orders'、'public.users'")
    schema_name: str = Field(default="public", description="Schema 名称")


@tool("pg_query", args_schema=PgQueryInput)
def pg_query(sql: str, max_rows: int = 50) -> str:
    """在 PostgreSQL 中执行只读 SQL 查询。适用于执行诊断查询，如查看连接数、锁等待、复制状态等。
    注意：仅支持 SELECT 查询，DML 操作会被拒绝。"""
    start_time = time.time()

    # 安全检查：拒绝写操作
    sql_upper = sql.strip().upper()
    forbidden = ["INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ", "TRUNCATE ",
                 "CREATE ", "GRANT ", "REVOKE ", "VACUUM "]
    for keyword in forbidden:
        if sql_upper.startswith(keyword):
            elapsed = time.time() - start_time
            return json.dumps(
                format_tool_result("pg_query", False, error=f"安全限制：不允许执行 {keyword.strip()} 操作"),
                ensure_ascii=False,
            )

    max_rows = min(max(max_rows, 1), 500)
    result = _execute_query(sql, max_rows=max_rows)
    elapsed = time.time() - start_time

    if "error" in result:
        return json.dumps(
            format_tool_result("pg_query", False, error=result["error"], elapsed=elapsed),
            ensure_ascii=False,
        )

    # 序列化特殊类型
    if "rows" in result:
        result["rows"] = _serialize_rows(result["rows"])

    return json.dumps(
        format_tool_result("pg_query", True, data={"sql": sql, **result}, elapsed=elapsed),
        ensure_ascii=False,
        default=str,
    )


@tool("pg_slow_queries", args_schema=PgSlowQueriesInput)
def pg_slow_queries(min_duration_ms: int = 1000, top_n: int = 10) -> str:
    """查询 PostgreSQL 当前正在运行的慢查询和活跃会话。
    返回执行时间超过阈值的 SQL、客户端信息、等待事件等，帮助定位数据库性能瓶颈。"""
    start_time = time.time()

    sql = """
        SELECT
            pid,
            usename AS username,
            client_addr,
            state,
            wait_event_type,
            wait_event,
            EXTRACT(EPOCH FROM (now() - query_start))::numeric(10,2) AS duration_seconds,
            left(query, 500) AS query_preview
        FROM pg_stat_activity
        WHERE state != 'idle'
          AND query NOT ILIKE '%%pg_stat_activity%%'
          AND EXTRACT(EPOCH FROM (now() - query_start)) * 1000 > %s
        ORDER BY duration_seconds DESC
        LIMIT %s
    """

    result = _execute_query(sql, params=(min_duration_ms, top_n))
    elapsed = time.time() - start_time

    if "error" in result:
        return json.dumps(
            format_tool_result("pg_slow_queries", False, error=result["error"], elapsed=elapsed),
            ensure_ascii=False,
        )

    if "rows" in result:
        result["rows"] = _serialize_rows(result["rows"])

    return json.dumps(
        format_tool_result("pg_slow_queries", True, data={
            "min_duration_ms": min_duration_ms,
            **result,
        }, elapsed=elapsed),
        ensure_ascii=False,
        default=str,
    )


@tool("pg_table_info", args_schema=PgTableInfoInput)
def pg_table_info(table_name: str, schema_name: str = "public") -> str:
    """查看 PostgreSQL 表的结构信息，包括列定义、索引、表大小、行数估算等。
    适用于了解表结构以编写诊断查询，或分析表膨胀问题。"""
    start_time = time.time()

    # 1. 列信息
    columns_sql = """
        SELECT column_name, data_type, is_nullable, column_default,
               character_maximum_length
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
    """
    columns_result = _execute_query(columns_sql, params=(schema_name, table_name))

    # 2. 索引信息
    indexes_sql = """
        SELECT indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = %s AND tablename = %s
    """
    indexes_result = _execute_query(indexes_sql, params=(schema_name, table_name))

    # 3. 表大小和行数
    size_sql = """
        SELECT
            pg_size_pretty(pg_total_relation_size(%s)) AS total_size,
            pg_size_pretty(pg_relation_size(%s)) AS table_size,
            pg_size_pretty(pg_indexes_size(%s)) AS index_size,
            (SELECT reltuples::bigint FROM pg_class WHERE relname = %s) AS estimated_rows
    """
    full_name = f"{schema_name}.{table_name}"
    size_result = _execute_query(size_sql, params=(full_name, full_name, full_name, table_name))

    elapsed = time.time() - start_time

    data = {
        "table": f"{schema_name}.{table_name}",
        "columns": columns_result.get("rows", []) if "error" not in columns_result else columns_result,
        "indexes": indexes_result.get("rows", []) if "error" not in indexes_result else indexes_result,
        "size": size_result.get("rows", [{}])[0] if "error" not in size_result and size_result.get("rows") else size_result,
    }

    # 序列化
    if isinstance(data["columns"], list):
        data["columns"] = _serialize_rows(data["columns"])
    if isinstance(data["indexes"], list):
        data["indexes"] = _serialize_rows(data["indexes"])
    if isinstance(data["size"], dict):
        data["size"] = {k: str(v) for k, v in data["size"].items()}

    has_error = any(
        isinstance(v, dict) and "error" in v
        for v in [data["columns"], data["indexes"], data["size"]]
    )

    return json.dumps(
        format_tool_result("pg_table_info", not has_error, data=data, elapsed=elapsed),
        ensure_ascii=False,
        default=str,
    )


def _serialize_rows(rows: list) -> list:
    """将查询结果中的特殊类型转为可 JSON 序列化的格式"""
    serialized = []
    for row in rows:
        new_row = {}
        for k, v in row.items():
            if hasattr(v, "isoformat"):
                new_row[k] = v.isoformat()
            elif isinstance(v, bytes):
                new_row[k] = v.hex()
            else:
                new_row[k] = v
        serialized.append(new_row)
    return serialized
