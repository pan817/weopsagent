"""
Elasticsearch MCP 工具 - 通过 Elasticsearch HTTP API 检索和聚合日志

提供日志搜索和聚合统计两个工具，替代 SSH 读取远程日志文件。
支持全文检索、时间范围过滤、关键词高亮、错误类型聚合等。

典型用法：
- 搜索最近 30 分钟的 ERROR 日志
- 统计各类异常出现频次
- 按服务名过滤日志并定位故障时间线
"""
import json
import logging
import time

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from config.settings import settings
from .client import mcp_request, format_mcp_result

logger = logging.getLogger(__name__)


class SearchLogsInput(BaseModel):
    """日志搜索输入"""
    query: str = Field(description="搜索关键词或 Lucene 查询语法，如 'ERROR order-service'、'level:ERROR AND service:order'")
    index: str = Field(default="*", description="Elasticsearch 索引名或模式，如 'app-logs-*'、'filebeat-2024.01.*'")
    time_range: str = Field(default="30m", description="时间范围，如 '15m'、'1h'、'6h'、'1d'")
    size: int = Field(default=50, description="返回条数上限（1-200）")
    fields: str = Field(default="", description="返回字段（逗号分隔），留空返回全部。如 'timestamp,level,message,service'")


class AggregateLogsInput(BaseModel):
    """日志聚合统计输入"""
    index: str = Field(default="*", description="Elasticsearch 索引名或模式")
    time_range: str = Field(default="1h", description="时间范围")
    group_by: str = Field(default="level", description="聚合字段，如 'level'、'service.keyword'、'error_type.keyword'")
    query: str = Field(default="*", description="过滤条件（Lucene 语法）")
    top_n: int = Field(default=20, description="返回前 N 个聚合桶")


@tool("search_logs", args_schema=SearchLogsInput)
def search_logs(
    query: str,
    index: str = "*",
    time_range: str = "30m",
    size: int = 50,
    fields: str = "",
) -> str:
    """在 Elasticsearch 中搜索日志。输入关键词和时间范围，返回匹配的日志条目。
    适用于故障排查时快速定位错误日志、追踪异常堆栈。"""
    start_time = time.time()

    base_url = settings.mcp_elasticsearch_url.rstrip("/")
    size = min(max(size, 1), 200)

    # 构建查询 DSL
    body = {
        "query": {
            "bool": {
                "must": [
                    {"query_string": {"query": query}},
                ],
                "filter": [
                    {"range": {"@timestamp": {"gte": f"now-{time_range}", "lte": "now"}}},
                ],
            }
        },
        "sort": [{"@timestamp": {"order": "desc"}}],
        "size": size,
    }

    # 指定返回字段
    if fields:
        body["_source"] = [f.strip() for f in fields.split(",")]

    auth = None
    if settings.mcp_elasticsearch_user and settings.mcp_elasticsearch_password:
        auth = (settings.mcp_elasticsearch_user, settings.mcp_elasticsearch_password)

    data = mcp_request(
        url=f"{base_url}/{index}/_search",
        method="POST",
        json_body=body,
        timeout=settings.mcp_elasticsearch_timeout,
        auth=auth,
    )

    elapsed = time.time() - start_time

    if isinstance(data, dict) and "hits" in data:
        hits = data["hits"]
        total = hits.get("total", {})
        total_count = total.get("value", 0) if isinstance(total, dict) else total
        records = [
            hit.get("_source", {}) for hit in hits.get("hits", [])
        ]
        return format_mcp_result("search_logs", {
            "query": query,
            "index": index,
            "time_range": time_range,
            "total_hits": total_count,
            "returned": len(records),
            "logs": records,
        }, elapsed)

    return format_mcp_result("search_logs", data, elapsed)


@tool("aggregate_logs", args_schema=AggregateLogsInput)
def aggregate_logs(
    index: str = "*",
    time_range: str = "1h",
    group_by: str = "level",
    query: str = "*",
    top_n: int = 20,
) -> str:
    """对 Elasticsearch 日志进行聚合统计。按指定字段分组统计日志数量。
    适用于了解错误分布、各服务日志量、异常类型 TOP N 等。"""
    start_time = time.time()

    base_url = settings.mcp_elasticsearch_url.rstrip("/")

    body = {
        "query": {
            "bool": {
                "must": [{"query_string": {"query": query}}],
                "filter": [
                    {"range": {"@timestamp": {"gte": f"now-{time_range}", "lte": "now"}}},
                ],
            }
        },
        "size": 0,
        "aggs": {
            "group_result": {
                "terms": {
                    "field": group_by,
                    "size": top_n,
                    "order": {"_count": "desc"},
                }
            }
        },
    }

    auth = None
    if settings.mcp_elasticsearch_user and settings.mcp_elasticsearch_password:
        auth = (settings.mcp_elasticsearch_user, settings.mcp_elasticsearch_password)

    data = mcp_request(
        url=f"{base_url}/{index}/_search",
        method="POST",
        json_body=body,
        timeout=settings.mcp_elasticsearch_timeout,
        auth=auth,
    )

    elapsed = time.time() - start_time

    if isinstance(data, dict) and "aggregations" in data:
        buckets = data["aggregations"].get("group_result", {}).get("buckets", [])
        total_docs = data.get("hits", {}).get("total", {})
        total_count = total_docs.get("value", 0) if isinstance(total_docs, dict) else total_docs
        return format_mcp_result("aggregate_logs", {
            "group_by": group_by,
            "time_range": time_range,
            "total_docs": total_count,
            "buckets": [{"key": b["key"], "count": b["doc_count"]} for b in buckets],
        }, elapsed)

    return format_mcp_result("aggregate_logs", data, elapsed)
