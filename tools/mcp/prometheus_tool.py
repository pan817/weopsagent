"""
Prometheus MCP 工具 - 通过 Prometheus HTTP API 查询监控指标

提供即时查询和范围查询两个工具，支持 PromQL 表达式。
可替代 SSH 采集方式，直接从 Prometheus 获取时序指标数据。

典型用法：
- 查询服务 CPU/内存使用率
- 检查接口延迟和错误率
- 查看 Redis/MQ/数据库连接数趋势
"""
import json
import logging
import time
from typing import Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from config.settings import settings
from .client import mcp_request, format_mcp_result

logger = logging.getLogger(__name__)


class PromQLInput(BaseModel):
    """Prometheus 即时查询输入"""
    query: str = Field(description="PromQL 查询表达式，如 'up{job=\"order-service\"}'、'rate(http_requests_total[5m])'")
    time: Optional[str] = Field(default=None, description="查询时间点（RFC3339 或 Unix 时间戳），默认为当前时间")


class PromQLRangeInput(BaseModel):
    """Prometheus 范围查询输入"""
    query: str = Field(description="PromQL 查询表达式")
    start: str = Field(description="起始时间（RFC3339 或 Unix 时间戳），如 '2024-01-01T00:00:00Z' 或相对时间 '-1h'")
    end: str = Field(default="now", description="结束时间，默认为当前时间")
    step: str = Field(default="60s", description="数据点间隔，如 '15s'、'1m'、'5m'")


@tool("query_prometheus", args_schema=PromQLInput)
def query_prometheus(query: str, time: Optional[str] = None) -> str:
    """查询 Prometheus 监控指标（即时查询）。输入 PromQL 表达式，返回当前时刻的指标值。
    适用于查看服务当前状态，如 CPU 使用率、连接数、错误率等。"""
    start_time = __import__("time").time()

    base_url = settings.mcp_prometheus_url.rstrip("/")
    params = {"query": query}
    if time:
        params["time"] = time

    data = mcp_request(
        url=f"{base_url}/api/v1/query",
        method="GET",
        params=params,
        timeout=settings.mcp_prometheus_timeout,
    )

    elapsed = __import__("time").time() - start_time

    # 提取结果摘要
    if isinstance(data, dict) and data.get("status") == "success":
        results = data.get("data", {}).get("result", [])
        summary = _format_instant_results(results)
        return format_mcp_result("query_prometheus", {
            "query": query,
            "result_count": len(results),
            "results": summary,
        }, elapsed)

    return format_mcp_result("query_prometheus", data, elapsed)


@tool("query_prometheus_range", args_schema=PromQLRangeInput)
def query_prometheus_range(
    query: str,
    start: str,
    end: str = "now",
    step: str = "60s",
) -> str:
    """查询 Prometheus 监控指标的时间范围数据。输入 PromQL 表达式和时间范围，返回趋势数据。
    适用于分析指标变化趋势、定位故障时间点。"""
    start_time = __import__("time").time()

    base_url = settings.mcp_prometheus_url.rstrip("/")
    # 处理相对时间
    if start.startswith("-"):
        import datetime
        delta_str = start.lstrip("-")
        now = datetime.datetime.now(datetime.timezone.utc)
        if delta_str.endswith("h"):
            delta = datetime.timedelta(hours=int(delta_str[:-1]))
        elif delta_str.endswith("m"):
            delta = datetime.timedelta(minutes=int(delta_str[:-1]))
        else:
            delta = datetime.timedelta(seconds=int(delta_str.rstrip("s")))
        start = (now - delta).isoformat()

    if end == "now":
        import datetime
        end = datetime.datetime.now(datetime.timezone.utc).isoformat()

    params = {"query": query, "start": start, "end": end, "step": step}

    data = mcp_request(
        url=f"{base_url}/api/v1/query_range",
        method="GET",
        params=params,
        timeout=settings.mcp_prometheus_timeout,
    )

    elapsed = __import__("time").time() - start_time

    if isinstance(data, dict) and data.get("status") == "success":
        results = data.get("data", {}).get("result", [])
        summary = _format_range_results(results)
        return format_mcp_result("query_prometheus_range", {
            "query": query,
            "time_range": f"{start} → {end}",
            "step": step,
            "series_count": len(results),
            "results": summary,
        }, elapsed)

    return format_mcp_result("query_prometheus_range", data, elapsed)


def _format_instant_results(results: list, max_items: int = 20) -> list:
    """格式化即时查询结果"""
    formatted = []
    for r in results[:max_items]:
        metric = r.get("metric", {})
        value = r.get("value", [None, None])
        formatted.append({
            "labels": metric,
            "value": value[1] if len(value) > 1 else None,
            "timestamp": value[0] if value else None,
        })
    return formatted


def _format_range_results(results: list, max_series: int = 10, max_points: int = 30) -> list:
    """格式化范围查询结果（截断过多数据点）"""
    formatted = []
    for r in results[:max_series]:
        metric = r.get("metric", {})
        values = r.get("values", [])
        # 等间隔采样，避免数据量过大
        if len(values) > max_points:
            step = len(values) // max_points
            sampled = values[::step][:max_points]
        else:
            sampled = values
        formatted.append({
            "labels": metric,
            "total_points": len(values),
            "sampled_points": len(sampled),
            "values": [{"t": v[0], "v": v[1]} for v in sampled],
        })
    return formatted
