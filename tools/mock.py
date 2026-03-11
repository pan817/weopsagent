"""
Mock 工具模块 - 所有工具的假实现，用于无真实环境时的调试

每个 mock 工具保持与真实工具相同的 @tool 名称、args_schema 和返回格式，
但直接返回模拟数据，不依赖 SSH / Redis / MQ / DB 等外部服务。

使用方式：
    在 .env 中设置 USE_MOCK_TOOLS=true，工具注册表将自动使用 mock 实现。
"""
import json
import logging
import time
from typing import List

from langchain_core.tools import tool

from .base import format_tool_result
from .process_monitor import ProcessMonitorInput
from .redis_monitor import RedisMonitorInput
from .mq_monitor import MQMonitorInput
from .db_monitor import DBMonitorInput
from .log_analyzer import LogAnalyzerInput
from .service_restart import ServiceRestartInput
from .notification import NotificationInput
from .knowledge_store import StoreKnowledgeInput

logger = logging.getLogger(__name__)


# ===== 1. monitor_process =====

@tool(
    "monitor_process",
    args_schema=ProcessMonitorInput,
    description="监控指定服务器上的服务进程状态，检查进程是否在运行、CPU 使用率、内存占用情况。需要提供目标主机 IP 和服务进程名称。",
)
def mock_monitor_process(
    host: str,
    service_name: str,
    port: int = None,
    username: str = None,
) -> str:
    """监控指定服务器上的服务进程状态。检查进程是否在运行、CPU 使用率、内存占用情况。"""
    logger.info(f"[MockTool] monitor_process host={host} service={service_name}")
    data = {
        "host": host,
        "service_name": service_name,
        "process_running": True,
        "process_list": (
            f"12345 java -jar {service_name}.jar --server.port=8080\n"
            f"12346 java -jar {service_name}.jar --server.port=8081"
        ),
        "resource_usage": "cpu=35.2% mem=28.7% count=2",
        "system_info": (
            " 10:30:00 up 45 days, 3:22, 2 users, load average: 1.25, 0.98, 0.87\n"
            "mem_total=16384MB mem_used=12288MB mem_free=4096MB"
        ),
    }
    return json.dumps(
        format_tool_result("monitor_process", True, data, elapsed=0.35),
        ensure_ascii=False,
    )


# ===== 2. monitor_redis =====

@tool(
    "monitor_redis",
    args_schema=RedisMonitorInput,
    description="监控 Redis 服务器运行状态，包括内存使用、连接数、命令统计、慢查询等。可快速判断 Redis 是否存在性能问题或连接异常。",
)
def mock_monitor_redis(
    host: str = None,
    port: int = None,
    password: str = None,
    check_keys_pattern: str = None,
) -> str:
    """监控 Redis 服务器的运行状态，包括内存使用、连接数、命令统计、慢查询等。可以快速判断 Redis 是否存在性能问题。"""
    target_host = host or "127.0.0.1"
    target_port = port or 6379
    logger.info(f"[MockTool] monitor_redis {target_host}:{target_port}")
    data = {
        "host": target_host,
        "port": target_port,
        "connected": True,
        "server_version": "7.2.4",
        "uptime_days": 120,
        "memory": {
            "used_memory_human": "256.50M",
            "used_memory_peak_human": "512.00M",
            "maxmemory_human": "2.00G",
            "mem_fragmentation_ratio": 1.12,
        },
        "connections": {
            "connected_clients": 45,
            "blocked_clients": 0,
            "rejected_connections": 0,
        },
        "stats": {
            "total_commands_processed": 15820300,
            "instantaneous_ops_per_sec": 1250,
            "keyspace_hits": 12500000,
            "keyspace_misses": 320000,
            "evicted_keys": 0,
            "expired_keys": 85000,
        },
        "total_keys": 128500,
        "slow_log": [
            {"id": 101, "duration_us": 15230, "command": "KEYS session:*"},
            {"id": 100, "duration_us": 12100, "command": "SMEMBERS large_set"},
        ],
        "pattern_key_count": 350 if check_keys_pattern else None,
        "replication_role": "master",
    }
    return json.dumps(
        format_tool_result("monitor_redis", True, data, elapsed=0.18),
        ensure_ascii=False,
    )


# ===== 3. monitor_mq =====

@tool(
    "monitor_mq",
    args_schema=MQMonitorInput,
    description="监控 RabbitMQ 消息队列状态，包括队列积压量、消费者数量、消息速率等。可发现消息堆积、消费者掉线等问题。",
)
def mock_monitor_mq(
    host: str = None,
    management_port: int = None,
    username: str = None,
    password: str = None,
    queue_name: str = None,
    vhost: str = "%2F",
) -> str:
    """监控 RabbitMQ 消息队列状态，包括队列积压量、消费者数量、消息速率等。可以发现消息堆积、消费者掉线等问题。"""
    target_host = host or "127.0.0.1"
    logger.info(f"[MockTool] monitor_mq {target_host}")
    congested = [
        {
            "name": "order.delay.queue",
            "messages": 5200,
            "messages_ready": 5200,
            "messages_unacknowledged": 0,
            "consumers": 0,
            "state": "running",
            "publish_rate": 120.5,
            "deliver_rate": 0,
        },
    ]
    top_queues = [
        {
            "name": "order.delay.queue",
            "messages": 5200,
            "messages_ready": 5200,
            "messages_unacknowledged": 0,
            "consumers": 0,
            "state": "running",
            "publish_rate": 120.5,
            "deliver_rate": 0,
        },
        {
            "name": "order.process.queue",
            "messages": 35,
            "messages_ready": 10,
            "messages_unacknowledged": 25,
            "consumers": 3,
            "state": "running",
            "publish_rate": 85.0,
            "deliver_rate": 82.3,
        },
        {
            "name": "notification.queue",
            "messages": 12,
            "messages_ready": 12,
            "messages_unacknowledged": 0,
            "consumers": 2,
            "state": "running",
            "publish_rate": 30.0,
            "deliver_rate": 29.8,
        },
    ]
    if queue_name:
        top_queues = [q for q in top_queues if queue_name in q["name"]] or top_queues[:1]

    data = {
        "host": target_host,
        "port": management_port or 15672,
        "connected": True,
        "rabbitmq_version": "3.13.1",
        "total_connections": 28,
        "total_queues": 15,
        "total_messages": 5247,
        "congested_queues": congested,
        "top_queues": top_queues,
        "message_stats": {
            "publish": 1250000,
            "deliver": 1244800,
            "ack": 1244500,
        },
    }
    return json.dumps(
        format_tool_result("monitor_mq", True, data, elapsed=0.22),
        ensure_ascii=False,
    )


# ===== 4. monitor_database =====

@tool(
    "monitor_database",
    args_schema=DBMonitorInput,
    description="监控 MySQL 数据库运行状态，包括连接池、慢查询、锁等待、InnoDB 缓冲池等。可快速发现数据库性能瓶颈和连接问题。",
)
def mock_monitor_database(
    host: str = None,
    port: int = None,
    username: str = None,
    password: str = None,
    database: str = None,
    slow_query_threshold_ms: int = 1000,
    top_n_slow_queries: int = 10,
) -> str:
    """监控数据库运行状态，包括连接池状态、慢查询、锁等待等。可以快速发现数据库性能瓶颈和连接问题。"""
    target_host = host or "127.0.0.1"
    target_db = database or "production"
    logger.info(f"[MockTool] monitor_database {target_host}/{target_db}")
    data = {
        "host": target_host,
        "port": port or 3306,
        "database": target_db,
        "connected": True,
        "db_version": "8.0.35",
        "total_connections": 85,
        "active_connections": 12,
        "max_connections": 500,
        "slow_queries": [
            {
                "id": 88901,
                "user": "app_user",
                "db": target_db,
                "command": "Query",
                "time_seconds": 8,
                "state": "Sending data",
                "info": (
                    "SELECT o.*, u.name FROM orders o "
                    "JOIN users u ON o.user_id = u.id "
                    "WHERE o.created_at > '2024-01-01' ORDER BY o.id DESC LIMIT 1000"
                ),
            },
            {
                "id": 88905,
                "user": "app_user",
                "db": target_db,
                "command": "Query",
                "time_seconds": 3,
                "state": "Sorting result",
                "info": "SELECT * FROM order_items WHERE order_id IN (SELECT id FROM orders WHERE status='pending')",
            },
        ],
        "lock_waits": [],
        "global_status": {
            "Innodb_buffer_pool_reads": "125000",
            "Innodb_buffer_pool_read_requests": "98500000",
            "Queries": "25800000",
            "Slow_queries": "156",
            "Threads_running": "12",
            "Threads_connected": "85",
        },
    }
    return json.dumps(
        format_tool_result("monitor_database", True, data, elapsed=0.28),
        ensure_ascii=False,
    )


# ===== 5. analyze_logs =====

@tool(
    "analyze_logs",
    args_schema=LogAnalyzerInput,
    description="读取并分析远程服务器上的日志文件，统计 ERROR/WARN/Exception 等报错信息，识别最频繁出现的异常类型和错误样本。",
)
def mock_analyze_logs(
    host: str,
    log_path: str,
    lines: int = 1000,
    error_keywords: List[str] = None,
    time_window_minutes: int = 30,
) -> str:
    """读取并分析远程服务器上的日志文件，统计各类报错信息，识别最频繁出现的异常类型。"""
    logger.info(f"[MockTool] analyze_logs host={host} path={log_path}")
    data = {
        "host": host,
        "log_path": log_path,
        "total_lines_analyzed": lines,
        "last_modified": "2024-12-15 10:28:30",
        "file_size": "125M",
        "error_summary": {
            "ERROR": 42,
            "TIMEOUT": 18,
            "Exception": 15,
            "WARNING": 89,
            "Connection refused": 7,
        },
        "error_samples": {
            "ERROR": [
                "2024-12-15 10:25:12 ERROR [order-service] Failed to process order #98765: Database connection timeout",
                "2024-12-15 10:26:45 ERROR [order-service] Order payment callback failed: HttpConnectionPool max retries exceeded",
                "2024-12-15 10:28:01 ERROR [order-service] Redis cache miss, fallback to DB query failed",
            ],
            "TIMEOUT": [
                "2024-12-15 10:24:30 TIMEOUT [http-pool] Request to payment-service timed out after 30000ms",
                "2024-12-15 10:27:15 TIMEOUT [db-pool] Query execution exceeded 5000ms threshold",
            ],
            "Connection refused": [
                "2024-12-15 10:25:55 ERROR Connection refused: redis://127.0.0.1:6379",
            ],
        },
        "recent_exceptions": [
            "java.sql.SQLTransientConnectionException: HikariPool-1 - Connection is not available, request timed out after 30000ms",
            "redis.clients.jedis.exceptions.JedisConnectionException: Could not get a resource from the pool",
            "java.util.concurrent.TimeoutException: Timeout waiting for task",
        ],
        "has_errors": True,
        "top_error": ("WARNING", 89),
    }
    return json.dumps(
        format_tool_result("analyze_logs", True, data, elapsed=0.45),
        ensure_ascii=False,
    )


# ===== 6. restart_service =====

@tool(
    "restart_service",
    args_schema=ServiceRestartInput,
    description="⚠️ 危险操作：通过 SSH 重启远程服务器上的指定服务。支持 systemctl/supervisorctl 自动检测，执行前需人工确认。",
)
def mock_restart_service(
    host: str,
    service_name: str,
    restart_command: str = None,
    pre_check_command: str = None,
    post_check_command: str = None,
) -> str:
    """⚠️ 危险操作：重启远程服务器上的指定服务。此操作会中断服务，执行前需要人工确认。"""
    logger.info(f"[MockTool] restart_service host={host} service={service_name}")
    cmd = restart_command or f"systemctl restart {service_name}"
    steps = []
    if pre_check_command:
        steps.append({
            "step": "pre_check",
            "command": pre_check_command,
            "output": f"{service_name} is running (pid: 12345)",
            "error": None,
        })
    steps.append({
        "step": "restart",
        "command": cmd,
        "exit_status": 0,
        "output": "",
        "error": None,
        "success": True,
    })
    steps.append({
        "step": "post_check_default",
        "output": f"12400 java -jar {service_name}.jar --server.port=8080",
        "service_running": True,
    })
    data = {
        "host": host,
        "service_name": service_name,
        "restart_command": cmd,
        "steps": steps,
        "restart_time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return json.dumps(
        format_tool_result("restart_service", True, data, elapsed=3.50),
        ensure_ascii=False,
    )


# ===== 7. send_notification =====

@tool(
    "send_notification",
    args_schema=NotificationInput,
    description="向运维人员发送故障通知消息，支持钉钉、Slack、邮件等多渠道。可用于故障告警、处理进展和恢复通知。",
)
def mock_send_notification(
    message: str,
    title: str = "WeOps 故障通知",
    severity: str = "warning",
    channels: List[str] = None,
    recipients: List[str] = None,
) -> str:
    """向运维人员发送通知消息，支持钉钉、Slack、邮件等多渠道。可用于故障告警、处理进展通知、故障恢复通知等场景。"""
    active_channels = channels or ["dingtalk", "email"]
    logger.info(f"[MockTool] send_notification channels={active_channels} severity={severity}")
    channel_results = {}
    for ch in active_channels:
        if ch == "dingtalk":
            channel_results["dingtalk"] = {"success": True, "response": {"errcode": 0, "errmsg": "ok"}}
        elif ch == "slack":
            channel_results["slack"] = {"success": True, "status_code": 200}
        elif ch == "email":
            channel_results["email"] = {
                "success": True,
                "recipients": recipients or ["ops@example.com"],
            }
    data = {
        "channels": channel_results,
        "message_preview": message[:200],
    }
    return json.dumps(
        format_tool_result("send_notification", True, data, elapsed=0.65),
        ensure_ascii=False,
    )


# ===== 8. store_knowledge =====

@tool(
    "store_knowledge",
    args_schema=StoreKnowledgeInput,
    description="将有效的故障处理措施存入知识库（ChromaDB 长期记忆），记录故障现象、根因和解决方案，供未来类似故障参考。",
)
def mock_store_knowledge(
    title: str,
    fault_description: str,
    root_cause: str,
    solution: str,
    category: str = "history",
    tags: str = "",
    effectiveness: str = "confirmed",
) -> str:
    """将有效的故障处理措施存入知识库（长期记忆），用于未来类似故障的参考。在故障成功处理后应调用此工具保存经验。"""
    logger.info(f"[MockTool] store_knowledge title={title} category={category}")
    doc_id = f"mock-{category}-{int(time.time())}"
    data = {
        "doc_id": doc_id,
        "title": title,
        "category": category,
    }
    return json.dumps(
        format_tool_result("store_knowledge", True, data, elapsed=0.12),
        ensure_ascii=False,
    )
