"""
故障规划器模块 - 解析故障描述，加载服务拓扑，制定分析计划

规划器负责：
1. 从故障描述中推断受影响的服务名称
2. 加载服务的全链路依赖拓扑（service_node/*.md）
3. 根据告警类型制定监控分析计划
"""
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import settings

logger = logging.getLogger(__name__)


class AlertType(str, Enum):
    """告警类型枚举"""
    SERVICE_UNSTABLE = "service_unstable"          # 服务不稳定
    API_SLOW = "api_slow"                           # 接口响应缓慢
    API_ERROR = "api_error"                         # 接口报错
    DATA_INCONSISTENCY = "data_inconsistency"       # 数据不一致
    SERVICE_DOWN = "service_down"                   # 服务宕机
    HIGH_CPU = "high_cpu"                           # CPU 使用率高
    HIGH_MEMORY = "high_memory"                     # 内存使用率高
    DB_SLOW_QUERY = "db_slow_query"                 # 数据库慢查询
    MQ_BACKLOG = "mq_backlog"                       # 消息队列积压
    REDIS_ERROR = "redis_error"                     # Redis 异常
    UNKNOWN = "unknown"                             # 未知类型


@dataclass
class ServiceNode:
    """服务节点信息，从 Markdown 文件解析"""
    service_name: str
    description: str = ""
    hosts: List[str] = field(default_factory=list)
    log_paths: List[str] = field(default_factory=list)
    redis_instances: List[Dict[str, Any]] = field(default_factory=list)
    databases: List[Dict[str, Any]] = field(default_factory=list)
    mq_queues: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    raw_content: str = ""


@dataclass
class FaultAnalysisPlan:
    """故障分析计划"""
    fault_id: str
    fault_description: str
    service_name: str
    alert_type: AlertType
    service_node: Optional[ServiceNode]
    monitoring_steps: List[str]
    knowledge_query: str
    raw_service_info: str


# 告警类型关键词映射
ALERT_KEYWORDS: Dict[AlertType, List[str]] = {
    AlertType.SERVICE_DOWN: [
        "宕机", "down", "不可用", "崩溃", "crash", "挂了", "无法连接", "connection refused",
    ],
    AlertType.SERVICE_UNSTABLE: [
        "不稳定", "抖动", "波动", "偶发", "间歇", "unstable", "flapping",
    ],
    AlertType.API_SLOW: [
        "响应慢", "超时", "timeout", "slow", "延迟高", "latency", "请求缓慢",
    ],
    AlertType.API_ERROR: [
        "报错", "500", "error", "exception", "异常", "失败率", "错误率",
    ],
    AlertType.DATA_INCONSISTENCY: [
        "数据不一致", "脏数据", "数据丢失", "数据异常", "inconsistent",
    ],
    AlertType.HIGH_CPU: [
        "cpu高", "cpu使用率", "cpu load", "负载高", "high cpu",
    ],
    AlertType.HIGH_MEMORY: [
        "内存", "oom", "out of memory", "内存溢出", "内存泄漏", "gc频繁",
    ],
    AlertType.DB_SLOW_QUERY: [
        "慢sql", "slow query", "数据库慢", "db slow", "查询超时",
    ],
    AlertType.MQ_BACKLOG: [
        "消息积压", "队列积压", "mq", "rabbit", "kafka", "消息堆积",
    ],
    AlertType.REDIS_ERROR: [
        "redis", "缓存", "cache", "redis超时", "redis连接",
    ],
}


class FaultPlanner:
    """
    故障规划器 - 解析故障上下文并制定分析计划
    """

    def __init__(self):
        self._service_nodes_cache: Dict[str, ServiceNode] = {}
        self._load_all_service_nodes()

    def _load_all_service_nodes(self) -> None:
        """预加载所有服务节点配置"""
        service_node_dir = settings.service_node_dir
        if not service_node_dir.exists():
            logger.warning(f"[FaultPlanner] service_node 目录不存在: {service_node_dir}")
            return

        for md_file in service_node_dir.glob("*.md"):
            service_name = md_file.stem
            node = self._parse_service_node_file(md_file, service_name)
            if node:
                self._service_nodes_cache[service_name.lower()] = node
                logger.debug(f"[FaultPlanner] 已加载服务节点: {service_name}")

        logger.info(f"[FaultPlanner] 共加载 {len(self._service_nodes_cache)} 个服务节点配置")

    def _parse_service_node_file(self, file_path: Path, service_name: str) -> Optional[ServiceNode]:
        """解析服务节点 Markdown 文件"""
        try:
            content = file_path.read_text(encoding="utf-8")
            node = ServiceNode(service_name=service_name, raw_content=content)

            # 解析主机地址
            host_matches = re.findall(
                r"(?:主机|host|服务器)[：:]\s*([0-9a-zA-Z.\-,\s]+)",
                content, re.IGNORECASE
            )
            for match in host_matches:
                hosts = [h.strip() for h in re.split(r"[,，\s]+", match) if h.strip()]
                node.hosts.extend(hosts)

            # 解析日志路径
            log_matches = re.findall(
                r"(?:日志路径|log.?path)[：:]\s*([/\w\-*.]+)",
                content, re.IGNORECASE
            )
            node.log_paths = [p.strip() for p in log_matches]

            # 解析 Redis 实例
            redis_matches = re.findall(
                r"redis[^:：\n]*[：:]\s*([0-9a-zA-Z.\-:]+)",
                content, re.IGNORECASE
            )
            for match in redis_matches:
                if ":" in match:
                    parts = match.split(":")
                    node.redis_instances.append({"host": parts[0], "port": int(parts[1])})
                else:
                    node.redis_instances.append({"host": match, "port": 6379})

            # 解析数据库
            db_matches = re.findall(
                r"(?:数据库|database|mysql|postgresql)[^:：\n]*[：:]\s*([0-9a-zA-Z.\-:/_]+)",
                content, re.IGNORECASE
            )
            for match in db_matches:
                if ":" in match:
                    parts = match.split(":")
                    node.databases.append({"host": parts[0], "port": int(parts[1]) if len(parts) > 1 else 3306})
                else:
                    node.databases.append({"host": match, "port": 3306})

            # 解析 MQ 队列
            mq_matches = re.findall(
                r"(?:队列|queue|mq)[^:：\n]*[：:]\s*([a-zA-Z0-9._\-]+)",
                content, re.IGNORECASE
            )
            node.mq_queues = [q.strip() for q in mq_matches]

            # 解析依赖服务
            dep_matches = re.findall(
                r"(?:依赖服务|dependencies)[^:：\n]*[：:]\s*([a-zA-Z0-9,\-_\s]+)",
                content, re.IGNORECASE
            )
            for match in dep_matches:
                deps = [d.strip() for d in re.split(r"[,，\s]+", match) if d.strip()]
                node.dependencies.extend(deps)

            # 提取描述（第一行非标题内容）
            lines = content.split("\n")
            for line in lines:
                line = line.strip()
                if line and not line.startswith("#") and len(line) > 5:
                    node.description = line[:200]
                    break

            return node
        except Exception as e:
            logger.error(f"[FaultPlanner] 解析服务节点文件失败 {file_path}: {e}")
            return None

    def infer_service_name(self, fault_description: str) -> str:
        """
        从故障描述中推断受影响的服务名称

        先精确匹配已知服务名，再模糊匹配。
        """
        desc_lower = fault_description.lower()

        # 精确匹配已知服务名
        for service_name in self._service_nodes_cache.keys():
            if service_name in desc_lower:
                logger.info(f"[FaultPlanner] 推断服务名: {service_name} (精确匹配)")
                return service_name

        # 常见服务名关键词映射
        service_keywords = {
            "order": "order-service",
            "订单": "order-service",
            "user": "user-service",
            "用户": "user-service",
            "payment": "payment-service",
            "支付": "payment-service",
            "inventory": "inventory-service",
            "库存": "inventory-service",
            "gateway": "api-gateway",
            "网关": "api-gateway",
            "auth": "auth-service",
            "认证": "auth-service",
            "notification": "notification-service",
            "通知": "notification-service",
        }

        for keyword, service in service_keywords.items():
            if keyword in desc_lower:
                logger.info(f"[FaultPlanner] 推断服务名: {service} (关键词: {keyword})")
                return service

        logger.warning("[FaultPlanner] 无法推断服务名，使用 unknown")
        return "unknown"

    def identify_alert_type(self, fault_description: str) -> AlertType:
        """从故障描述中识别告警类型"""
        desc_lower = fault_description.lower()

        for alert_type, keywords in ALERT_KEYWORDS.items():
            for kw in keywords:
                if kw.lower() in desc_lower:
                    logger.info(f"[FaultPlanner] 识别告警类型: {alert_type} (关键词: {kw})")
                    return alert_type

        return AlertType.UNKNOWN

    def get_service_node(self, service_name: str) -> Optional[ServiceNode]:
        """获取服务节点配置"""
        return self._service_nodes_cache.get(service_name.lower())

    def format_service_info(self, service_node: Optional[ServiceNode]) -> str:
        """格式化服务节点信息用于 Prompt"""
        if not service_node:
            return "（未找到服务节点配置，无法获取全链路依赖信息）"

        lines = [
            f"## 服务名称: {service_node.service_name}",
            f"描述: {service_node.description}" if service_node.description else "",
        ]

        if service_node.hosts:
            lines.append(f"**服务器主机**: {', '.join(service_node.hosts)}")
        if service_node.log_paths:
            lines.append(f"**日志路径**: {', '.join(service_node.log_paths)}")
        if service_node.redis_instances:
            redis_info = [f"{r['host']}:{r['port']}" for r in service_node.redis_instances]
            lines.append(f"**Redis 实例**: {', '.join(redis_info)}")
        if service_node.databases:
            db_info = [f"{d['host']}:{d['port']}" for d in service_node.databases]
            lines.append(f"**数据库实例**: {', '.join(db_info)}")
        if service_node.mq_queues:
            lines.append(f"**MQ 队列**: {', '.join(service_node.mq_queues)}")
        if service_node.dependencies:
            lines.append(f"**依赖服务**: {', '.join(service_node.dependencies)}")

        return "\n".join(l for l in lines if l)

    def create_plan(
        self,
        fault_id: str,
        fault_description: str,
    ) -> FaultAnalysisPlan:
        """
        根据故障描述创建分析计划

        Args:
            fault_id: 故障唯一 ID
            fault_description: 故障描述文本

        Returns:
            FaultAnalysisPlan: 完整的故障分析计划
        """
        service_name = self.infer_service_name(fault_description)
        alert_type = self.identify_alert_type(fault_description)
        service_node = self.get_service_node(service_name)
        service_info = self.format_service_info(service_node)

        # 根据告警类型制定监控步骤
        monitoring_steps = self._build_monitoring_steps(alert_type, service_node)

        # 构建知识库检索查询
        knowledge_query = f"{fault_description} {alert_type.value} {service_name}"

        logger.info(
            f"[FaultPlanner] 已创建故障分析计划 "
            f"fault_id={fault_id} service={service_name} alert_type={alert_type}"
        )

        return FaultAnalysisPlan(
            fault_id=fault_id,
            fault_description=fault_description,
            service_name=service_name,
            alert_type=alert_type,
            service_node=service_node,
            monitoring_steps=monitoring_steps,
            knowledge_query=knowledge_query,
            raw_service_info=service_info,
        )

    def _build_monitoring_steps(
        self,
        alert_type: AlertType,
        service_node: Optional[ServiceNode],
    ) -> List[str]:
        """根据告警类型构建监控步骤"""
        steps = []

        # 基础监控步骤（所有类型都执行）
        if service_node and service_node.hosts:
            for host in service_node.hosts[:2]:  # 最多检查 2 台
                steps.append(f"monitor_process: host={host}")
                if service_node.log_paths:
                    for log_path in service_node.log_paths[:2]:
                        steps.append(f"analyze_logs: host={host}, log_path={log_path}")

        # 数据库相关
        if alert_type in (AlertType.API_SLOW, AlertType.DB_SLOW_QUERY, AlertType.DATA_INCONSISTENCY):
            steps.append("monitor_database: check_slow_queries")

        # Redis 相关
        if alert_type in (AlertType.API_SLOW, AlertType.REDIS_ERROR, AlertType.SERVICE_UNSTABLE):
            steps.append("monitor_redis: check_memory_and_connections")

        # MQ 相关
        if alert_type in (AlertType.MQ_BACKLOG, AlertType.DATA_INCONSISTENCY):
            steps.append("monitor_mq: check_queue_backlog")

        # 服务宕机
        if alert_type == AlertType.SERVICE_DOWN:
            steps.append("monitor_process: check_all_instances")
            steps.append("analyze_logs: check_startup_errors")

        # 内存溢出
        if alert_type == AlertType.HIGH_MEMORY:
            steps.append("monitor_process: check_memory_usage")
            steps.append("analyze_logs: grep_oom_errors")

        return steps or ["monitor_process", "analyze_logs", "monitor_database", "monitor_redis", "monitor_mq"]
