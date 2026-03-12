"""
MCP 工具模块 - 通过 Model Context Protocol 集成外部服务

将 MCP Server 暴露的能力封装为 LangChain @tool，
统一纳入 ToolRegistry 管理，供 Agent 调用。

已集成的 MCP Server：
- Prometheus: 指标查询（PromQL）
- Elasticsearch: 日志检索与聚合
- Kubernetes: 集群资源管理与诊断
- DingTalk: 钉钉消息通知
- PostgreSQL: 数据库查询与诊断
"""
from typing import List, Any

from config.settings import settings


def get_mcp_tools() -> List[Any]:
    """
    获取所有已启用的 MCP 工具列表

    根据 settings 中各 MCP Server 的连接配置判断是否启用，
    仅返回配置了有效连接信息的 MCP 工具。
    """
    tools = []

    if settings.mcp_prometheus_url:
        from .prometheus_tool import query_prometheus, query_prometheus_range
        tools.extend([query_prometheus, query_prometheus_range])

    if settings.mcp_elasticsearch_url:
        from .elasticsearch_tool import search_logs, aggregate_logs
        tools.extend([search_logs, aggregate_logs])

    if settings.mcp_kubernetes_enabled:
        from .kubernetes_tool import (
            k8s_get_pods, k8s_get_pod_logs, k8s_restart_deployment, k8s_describe_resource,
        )
        tools.extend([k8s_get_pods, k8s_get_pod_logs, k8s_restart_deployment, k8s_describe_resource])

    if settings.mcp_dingtalk_webhook:
        from .dingtalk_tool import dingtalk_send_text, dingtalk_send_markdown
        tools.extend([dingtalk_send_text, dingtalk_send_markdown])

    if settings.mcp_postgres_dsn:
        from .postgres_tool import pg_query, pg_slow_queries, pg_table_info
        tools.extend([pg_query, pg_slow_queries, pg_table_info])

    return tools


__all__ = ["get_mcp_tools"]
