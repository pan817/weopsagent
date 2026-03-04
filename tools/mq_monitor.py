"""
消息队列监控工具 - 支持 RabbitMQ 状态监控
"""
import json
import logging
import time
from typing import Any, Optional, Type

import httpx
from langchain_core.tools import BaseTool
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


class MQMonitorTool(BaseTool):
    """消息队列状态监控工具

    通过 RabbitMQ Management API 收集队列状态信息，包括：
    - 队列积压消息数量
    - 消费者数量
    - 消息发布/消费速率
    - 连接和通道统计
    """
    name: str = "monitor_mq"
    description: str = (
        "监控 RabbitMQ 消息队列状态，包括队列积压量、消费者数量、"
        "消息速率等。可以发现消息堆积、消费者掉线等问题。"
        "输入参数: host（MQ 地址，可选）, queue_name（队列名，可选）"
    )
    args_schema: Type[BaseModel] = MQMonitorInput

    def _run(
        self,
        host: str = None,
        management_port: int = None,
        username: str = None,
        password: str = None,
        queue_name: str = None,
        vhost: str = "%2F",
    ) -> str:
        """执行 MQ 监控"""
        start_time = time.time()
        target_host = host or settings.monitor_mq_host
        target_port = management_port or settings.monitor_mq_management_port
        target_user = username or settings.monitor_mq_user
        target_pass = password or settings.monitor_mq_password

        try:
            result = self._collect_mq_info(
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
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        queue_name: Optional[str],
        vhost: str,
    ) -> dict:
        """通过 Management API 收集 MQ 信息"""
        base_url = f"http://{host}:{port}/api"
        auth = (username, password)

        with httpx.Client(timeout=10.0) as client:
            # 获取集群概览
            overview_resp = client.get(f"{base_url}/overview", auth=auth)
            overview_resp.raise_for_status()
            overview = overview_resp.json()

            # 获取所有队列
            queues_resp = client.get(
                f"{base_url}/queues/{vhost}", auth=auth
            )
            queues_resp.raise_for_status()
            queues = queues_resp.json()

            # 获取所有连接数
            connections_resp = client.get(f"{base_url}/connections", auth=auth)
            connections_count = len(connections_resp.json()) if connections_resp.status_code == 200 else -1

        # 提取关键队列指标
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
            # 添加消息速率
            if "message_stats" in q:
                stats = q["message_stats"]
                q_info["publish_rate"] = stats.get("publish_details", {}).get("rate", 0)
                q_info["deliver_rate"] = stats.get("deliver_details", {}).get("rate", 0)
            queue_summary.append(q_info)

        # 识别积压最严重的队列
        congested_queues = [
            q for q in queue_summary
            if q["messages"] > 1000 or q["consumers"] == 0
        ]

        # 如果指定了特定队列，只返回该队列信息
        if queue_name:
            target_queues = [q for q in queue_summary if queue_name in q["name"]]
        else:
            # 返回前 10 个积压最多的队列
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

    async def _arun(self, *args, **kwargs) -> str:
        return self._run(*args, **kwargs)
