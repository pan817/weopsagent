"""
消息队列监控工具 - 支持 RabbitMQ 状态监控

使用 LangChain 1.2.x @tool 装饰器实现，不再继承 BaseTool 类。
"""
import json
import logging
import time
from typing import Optional

import httpx
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from config.settings import settings
from .base import format_tool_result, with_retry

logger = logging.getLogger(__name__)


class MQMonitorInput(BaseModel):
    """MQ 监控工具输入参数"""
    host: str = Field(default=None, description="MQ 服务器地址")
    management_port: int = Field(default=None, description="RabbitMQ Management API 端口")
    username: str = Field(default=None, description="MQ 管理用户名")
    password: str = Field(default=None, description="MQ 管理密码")
    queue_name: str = Field(default=None, description="要检查的特定队列名称（可选）")
    vhost: str = Field(default="%2F", description="Virtual Host，默认为 /")


@tool("monitor_mq", args_schema=MQMonitorInput)
def monitor_mq(
    host: str = None,
    management_port: int = None,
    username: str = None,
    password: str = None,
    queue_name: str = None,
    vhost: str = "%2F",
) -> str:
    """监控 RabbitMQ 消息队列状态，包括队列积压量、消费者数量、消息速率等。可以发现消息堆积、消费者掉线等问题。"""
    start_time = time.time()
    target_host = host or settings.monitor_mq_host
    target_port = management_port or settings.monitor_mq_management_port
    target_user = username or settings.monitor_mq_user
    target_pass = password or settings.monitor_mq_password

    try:
        result = _collect_mq_info(
            host=target_host,
            port=target_port,
            username=target_user,
            password=target_pass,
            queue_name=queue_name,
            vhost=vhost,
        )
        elapsed = time.time() - start_time
        return json.dumps(
            format_tool_result("monitor_mq", True, result, elapsed=elapsed),
            ensure_ascii=False,
        )
    except Exception as e:
        elapsed = time.time() - start_time
        logger.error(f"[MQMonitor] 监控失败: {e}")
        return json.dumps(
            format_tool_result("monitor_mq", False, error=str(e), elapsed=elapsed),
            ensure_ascii=False,
        )


@with_retry(exceptions=(httpx.ConnectError, httpx.TimeoutException))
def _collect_mq_info(
    host: str,
    port: int,
    username: str,
    password: str,
    queue_name: Optional[str],
    vhost: str,
) -> dict:
    """通过 Management API 收集 MQ 信息（带重试）"""
    base_url = f"http://{host}:{port}/api"
    auth = (username, password)

    with httpx.Client(timeout=10.0) as client:
        overview_resp = client.get(f"{base_url}/overview", auth=auth)
        overview_resp.raise_for_status()
        overview = overview_resp.json()

        queues_resp = client.get(f"{base_url}/queues/{vhost}", auth=auth)
        queues_resp.raise_for_status()
        queues = queues_resp.json()

        connections_resp = client.get(f"{base_url}/connections", auth=auth)
        connections_count = (
            len(connections_resp.json()) if connections_resp.status_code == 200 else -1
        )

    queue_summary = []
    for q in queues:
        q_info = {
            "name": q.get("name"),
            "messages": q.get("messages", 0),
            "messages_ready": q.get("messages_ready", 0),
            "messages_unacknowledged": q.get("messages_unacknowledged", 0),
            "consumers": q.get("consumers", 0),
            "state": q.get("state"),
        }
        if "message_stats" in q:
            stats = q["message_stats"]
            q_info["publish_rate"] = stats.get("publish_details", {}).get("rate", 0)
            q_info["deliver_rate"] = stats.get("deliver_details", {}).get("rate", 0)
        queue_summary.append(q_info)

    congested_queues = [
        q for q in queue_summary
        if q["messages"] > 1000 or q["consumers"] == 0
    ]

    if queue_name:
        target_queues = [q for q in queue_summary if queue_name in q["name"]]
    else:
        target_queues = sorted(queue_summary, key=lambda x: x["messages"], reverse=True)[:10]

    return {
        "host": host,
        "port": port,
        "connected": True,
        "rabbitmq_version": overview.get("rabbitmq_version"),
        "total_connections": connections_count,
        "total_queues": len(queues),
        "total_messages": sum(q["messages"] for q in queue_summary),
        "congested_queues": congested_queues[:5],
        "top_queues": target_queues,
        "message_stats": overview.get("message_stats", {}),
    }
