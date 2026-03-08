"""
故障规划器模块 - 解析故障描述，加载服务拓扑，制定分析计划

规划器职责（精简版）：
1. 从故障描述中推断受影响的服务名称
2. 加载服务的全链路依赖拓扑（service_node/*.md）
3. 将原始故障描述直接用作知识库检索 query（避免关键词误分类污染 RAG 结果）

移除的功能（方案B+C）：
- alert_type 关键词分类：分类结果未被任何 Agent 使用，且误分类会污染知识库检索
- monitoring_steps 构建：生成后从未传入 FaultState，是死代码
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
    """
    告警类型枚举（仅用于 API 返回值类型标注，不再做自动分类）

    当前所有故障均以 UNKNOWN 处理，由 MonitorAgent + AnalysisAgent 自行推断真实类型。
    保留此枚举是为了保持 FaultState 和 API 返回结构的向后兼容。
    """
    UNKNOWN = "unknown"


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
    alert_type: AlertType          # 固定为 UNKNOWN，由下游 Agent 自行判断
    service_node: Optional[ServiceNode]
    knowledge_query: str           # 直接使用原始故障描述，不拼接推断字段
    raw_service_info: str


class FaultPlanner:
    """
    故障规划器 - 解析故障上下文并制定分析计划

    核心职责：根据故障描述找到对应的服务拓扑配置文件，
    为后续 Agent 提供准确的主机地址、中间件连接信息等结构化数据。
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

        先精确匹配已知服务名，再模糊匹配常见服务关键词。
        """
        desc_lower = fault_description.lower()

        # 精确匹配已知服务名（来自 service_node/*.md 文件名）
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
            FaultAnalysisPlan: 包含服务拓扑信息和 RAG 检索 query 的分析计划
        """
        service_name = self.infer_service_name(fault_description)
        service_node = self.get_service_node(service_name)
        service_info = self.format_service_info(service_node)

        # 直接使用原始故障描述作为知识库检索 query
        # 避免拼接可能错误的 alert_type/service_name 污染 RAG 语义检索
        knowledge_query = fault_description

        logger.info(
            f"[FaultPlanner] 已创建故障分析计划 "
            f"fault_id={fault_id} service={service_name}"
        )

        return FaultAnalysisPlan(
            fault_id=fault_id,
            fault_description=fault_description,
            service_name=service_name,
            alert_type=AlertType.UNKNOWN,
            service_node=service_node,
            knowledge_query=knowledge_query,
            raw_service_info=service_info,
        )
