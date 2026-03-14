"""
Mock 工具模块 - 所有工具的假实现，用于无真实环境时的调试

每个 mock 工具保持与真实工具相同的 @tool 名称、args_schema 和返回格式，
但直接返回模拟数据，不依赖 SSH / Redis / MQ / DB 等外部服务。

使用方式：
    在 .env 中设置 USE_MOCK_TOOLS=true，工具注册表将自动使用 mock 实现。
"""
import json
import logging
import random
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
    cpu = round(random.uniform(5.0, 98.0), 1)
    mem = round(random.uniform(10.0, 95.0), 1)
    load1 = round(random.uniform(0.1, 12.0), 2)
    load5 = round(random.uniform(0.1, load1 * 1.2), 2)
    load15 = round(random.uniform(0.1, load5 * 1.1), 2)
    mem_total = 16384
    mem_used = int(mem_total * mem / 100)
    mem_free = mem_total - mem_used
    uptime_days = random.randint(1, 180)
    pid1 = random.randint(10000, 60000)
    pid2 = pid1 + random.randint(1, 5)
    data = {
        "host": host,
        "service_name": service_name,
        "process_running": True,
        "process_list": (
            f"{pid1} java -jar {service_name}.jar --server.port=8080\n"
            f"{pid2} java -jar {service_name}.jar --server.port=8081"
        ),
        "resource_usage": f"cpu={cpu}% mem={mem}% count=2",
        "system_info": (
            f" {time.strftime('%H:%M:%S')} up {uptime_days} days, load average: {load1}, {load5}, {load15}\n"
            f"mem_total={mem_total}MB mem_used={mem_used}MB mem_free={mem_free}MB"
        ),
    }
    return json.dumps(
        format_tool_result("monitor_process", True, data, elapsed=round(random.uniform(0.2, 0.6), 2)),
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
    max_clients = 500
    connected_clients = random.randint(1, max_clients)
    blocked = random.randint(0, max(0, connected_clients // 50))
    rejected = random.randint(0, 20) if connected_clients > 480 else 0
    used_mb = round(random.uniform(32, 1900), 1)
    peak_mb = round(used_mb * random.uniform(1.0, 1.5), 1)
    ops = random.randint(100, 8000)
    hits = random.randint(500000, 20000000)
    misses = random.randint(10000, hits // 10)
    evicted = random.randint(0, 5000) if used_mb > 1500 else 0
    slow_count = random.randint(0, 8)
    slow_commands = ["KEYS session:*", "SMEMBERS large_set", "LRANGE queue 0 -1", "ZRANGEBYSCORE leaderboard 0 +inf"]
    slow_log = [
        {"id": 100 + i, "duration_us": random.randint(5000, 50000), "command": random.choice(slow_commands)}
        for i in range(slow_count)
    ]
    data = {
        "host": target_host,
        "port": target_port,
        "connected": True,
        "server_version": "7.2.4",
        "uptime_days": random.randint(1, 300),
        "memory": {
            "used_memory_human": f"{used_mb}M",
            "used_memory_peak_human": f"{peak_mb}M",
            "maxmemory_human": "2.00G",
            "mem_fragmentation_ratio": round(random.uniform(0.9, 2.5), 2),
        },
        "connections": {
            "connected_clients": connected_clients,
            "blocked_clients": blocked,
            "rejected_connections": rejected,
        },
        "stats": {
            "total_commands_processed": random.randint(1000000, 50000000),
            "instantaneous_ops_per_sec": ops,
            "keyspace_hits": hits,
            "keyspace_misses": misses,
            "evicted_keys": evicted,
            "expired_keys": random.randint(10000, 200000),
        },
        "total_keys": random.randint(50000, 500000),
        "slow_log": slow_log,
        "pattern_key_count": random.randint(100, 1000) if check_keys_pattern else None,
        "replication_role": "master",
    }
    return json.dumps(
        format_tool_result("monitor_redis", True, data, elapsed=round(random.uniform(0.1, 0.4), 2)),
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

    def _make_queue(name, backlog_range=(0, 100), consumer_range=(1, 5)):
        msgs = random.randint(*backlog_range)
        unacked = random.randint(0, min(msgs, 50))
        ready = msgs - unacked
        consumers = random.randint(*consumer_range)
        pub = round(random.uniform(10, 200), 1)
        dlv = round(pub * random.uniform(0.0, 1.05), 1) if consumers > 0 else 0
        return {
            "name": name,
            "messages": msgs,
            "messages_ready": ready,
            "messages_unacknowledged": unacked,
            "consumers": consumers,
            "state": "running",
            "publish_rate": pub,
            "deliver_rate": dlv,
        }

    top_queues = [
        _make_queue("order.delay.queue",   backlog_range=(0, 8000), consumer_range=(0, 3)),
        _make_queue("order.process.queue", backlog_range=(0, 500),  consumer_range=(1, 6)),
        _make_queue("notification.queue",  backlog_range=(0, 100),  consumer_range=(1, 4)),
    ]
    if queue_name:
        top_queues = [q for q in top_queues if queue_name in q["name"]] or top_queues[:1]

    congested = [q for q in top_queues if q["messages"] > 1000 or q["consumers"] == 0]
    total_msgs = sum(q["messages"] for q in top_queues)
    publish_total = random.randint(800000, 2000000)
    data = {
        "host": target_host,
        "port": management_port or 15672,
        "connected": True,
        "rabbitmq_version": "3.13.1",
        "total_connections": random.randint(5, 80),
        "total_queues": random.randint(8, 25),
        "total_messages": total_msgs,
        "congested_queues": congested,
        "top_queues": top_queues,
        "message_stats": {
            "publish": publish_total,
            "deliver": int(publish_total * random.uniform(0.92, 1.0)),
            "ack": int(publish_total * random.uniform(0.90, 0.99)),
        },
    }
    return json.dumps(
        format_tool_result("monitor_mq", True, data, elapsed=round(random.uniform(0.1, 0.4), 2)),
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
    max_conn = 500
    total_conn = random.randint(5, max_conn)
    active_conn = random.randint(1, max(1, total_conn // 3))
    slow_sqls = [
        ("SELECT o.*, u.name FROM orders o JOIN users u ON o.user_id=u.id WHERE o.created_at>'2024-01-01' ORDER BY o.id DESC LIMIT 1000", "Sending data"),
        ("SELECT * FROM order_items WHERE order_id IN (SELECT id FROM orders WHERE status='pending')", "Sorting result"),
        ("UPDATE inventory SET stock=stock-1 WHERE product_id=? AND stock>0", "Updating"),
        ("SELECT COUNT(*) FROM user_logs WHERE created_at > NOW() - INTERVAL 1 HOUR", "Sending data"),
        ("DELETE FROM sessions WHERE expires_at < NOW()", "Removing"),
    ]
    slow_count = random.randint(0, min(top_n_slow_queries, len(slow_sqls)))
    slow_queries = [
        {
            "id": 88900 + i,
            "user": "app_user",
            "db": target_db,
            "command": "Query",
            "time_seconds": random.randint(1, 30),
            "state": slow_sqls[i][1],
            "info": slow_sqls[i][0],
        }
        for i in random.sample(range(len(slow_sqls)), slow_count)
    ]
    lock_count = random.randint(0, 3) if total_conn > 400 else 0
    lock_waits = [
        {"waiting_thread": random.randint(100, 999), "blocking_thread": random.randint(100, 999), "wait_seconds": random.randint(1, 60)}
        for _ in range(lock_count)
    ]
    bp_reads = random.randint(1000, 500000)
    bp_requests = random.randint(bp_reads * 100, bp_reads * 1000)
    queries_total = random.randint(1000000, 100000000)
    data = {
        "host": target_host,
        "port": port or 3306,
        "database": target_db,
        "connected": True,
        "db_version": "8.0.35",
        "total_connections": total_conn,
        "active_connections": active_conn,
        "max_connections": max_conn,
        "slow_queries": slow_queries,
        "lock_waits": lock_waits,
        "global_status": {
            "Innodb_buffer_pool_reads": str(bp_reads),
            "Innodb_buffer_pool_read_requests": str(bp_requests),
            "Queries": str(queries_total),
            "Slow_queries": str(random.randint(0, 500)),
            "Threads_running": str(active_conn),
            "Threads_connected": str(total_conn),
        },
    }
    return json.dumps(
        format_tool_result("monitor_database", True, data, elapsed=round(random.uniform(0.15, 0.5), 2)),
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
    ts_base = time.strftime("%Y-%m-%d %H")
    err_count = random.randint(0, 120)
    timeout_count = random.randint(0, 60)
    exc_count = random.randint(0, 40)
    warn_count = random.randint(0, 200)
    conn_refused = random.randint(0, 30)
    oom_count = random.randint(0, 5)

    all_error_samples = [
        f"{ts_base}:{random.randint(10,59)}:{random.randint(10,59)} ERROR [order-service] Failed to process order #{random.randint(10000,99999)}: Database connection timeout",
        f"{ts_base}:{random.randint(10,59)}:{random.randint(10,59)} ERROR [order-service] Order payment callback failed: HttpConnectionPool max retries exceeded",
        f"{ts_base}:{random.randint(10,59)}:{random.randint(10,59)} ERROR [order-service] Redis cache miss, fallback to DB query failed",
        f"{ts_base}:{random.randint(10,59)}:{random.randint(10,59)} ERROR [order-service] Null pointer exception in OrderController.createOrder()",
    ]
    all_timeout_samples = [
        f"{ts_base}:{random.randint(10,59)}:{random.randint(10,59)} TIMEOUT [http-pool] Request to payment-service timed out after {random.randint(5,60)*1000}ms",
        f"{ts_base}:{random.randint(10,59)}:{random.randint(10,59)} TIMEOUT [db-pool] Query execution exceeded {random.randint(3,10)*1000}ms threshold",
    ]
    all_conn_samples = [
        f"{ts_base}:{random.randint(10,59)}:{random.randint(10,59)} ERROR Connection refused: redis://127.0.0.1:6379",
        f"{ts_base}:{random.randint(10,59)}:{random.randint(10,59)} ERROR Connection refused: mysql://{host}:3306",
    ]
    all_exc_pool = [
        "java.sql.SQLTransientConnectionException: HikariPool-1 - Connection is not available, request timed out after 30000ms",
        "redis.clients.jedis.exceptions.JedisConnectionException: Could not get a resource from the pool",
        "java.util.concurrent.TimeoutException: Timeout waiting for task",
        "java.lang.OutOfMemoryError: Java heap space",
        "org.springframework.dao.DataAccessException: Unable to acquire JDBC Connection",
    ]

    error_summary = {"ERROR": err_count, "TIMEOUT": timeout_count, "Exception": exc_count, "WARNING": warn_count}
    if conn_refused:
        error_summary["Connection refused"] = conn_refused
    if oom_count:
        error_summary["OOM"] = oom_count

    top_error = max(error_summary, key=error_summary.get)
    error_samples = {}
    if err_count:
        error_samples["ERROR"] = random.sample(all_error_samples, min(3, len(all_error_samples)))
    if timeout_count:
        error_samples["TIMEOUT"] = random.sample(all_timeout_samples, min(2, len(all_timeout_samples)))
    if conn_refused:
        error_samples["Connection refused"] = random.sample(all_conn_samples, min(2, len(all_conn_samples)))

    recent_exc_count = random.randint(0, min(3, len(all_exc_pool)))
    file_size_mb = random.randint(10, 500)
    data = {
        "host": host,
        "log_path": log_path,
        "total_lines_analyzed": lines,
        "last_modified": f"{ts_base}:{random.randint(10,59)}:{random.randint(10,59)}",
        "file_size": f"{file_size_mb}M",
        "error_summary": error_summary,
        "error_samples": error_samples,
        "recent_exceptions": random.sample(all_exc_pool, recent_exc_count),
        "has_errors": any(v > 0 for v in error_summary.values()),
        "top_error": (top_error, error_summary[top_error]),
    }
    return json.dumps(
        format_tool_result("analyze_logs", True, data, elapsed=round(random.uniform(0.3, 0.8), 2)),
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
    new_pid = random.randint(10000, 60000)
    restart_elapsed = round(random.uniform(1.5, 8.0), 2)
    steps = []
    if pre_check_command:
        old_pid = random.randint(10000, 60000)
        steps.append({
            "step": "pre_check",
            "command": pre_check_command,
            "output": f"{service_name} is running (pid: {old_pid})",
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
        "output": f"{new_pid} java -jar {service_name}.jar --server.port=8080",
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
        format_tool_result("restart_service", True, data, elapsed=restart_elapsed),
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
    service: str = "",
    alert_type: str = "",
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
