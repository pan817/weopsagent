"""
故障规划器模块 - 使用 LLM 意图识别解析故障描述，加载服务拓扑，制定分析计划

规划器职责：
1. 使用 LLM 从故障描述中识别受影响的服务名称和告警类型（结构化输出）
2. 加载服务的全链路依赖拓扑（service_node/*.md）
3. 由 LLM 生成优化后的知识库检索 query

LLM 意图识别优势（对比旧版关键词匹配）：
- 自然语言理解：「订单系统偶尔报500」→ service=order-service, alert=http_error
- 自动适配新服务：只需在 service_node/ 下添加 md 文件，无需维护关键词映射
- 一次调用同时输出 service_name + alert_type + knowledge_query
"""
import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import settings

logger = logging.getLogger(__name__)


class AlertType(str, Enum):
    """告警类型枚举，由 LLM 意图识别自动分类"""
    TIMEOUT = "timeout"                 # 超时类：接口超时、连接超时
    CONNECTION = "connection"           # 连接类：连接拒绝、连接池耗尽
    HTTP_ERROR = "http_error"           # HTTP 错误：5xx、4xx
    OOM = "oom"                         # 内存溢出：OOM、内存不足
    HIGH_CPU = "high_cpu"               # CPU 过高
    HIGH_LOAD = "high_load"             # 负载过高
    DISK_FULL = "disk_full"             # 磁盘满
    PROCESS_DOWN = "process_down"       # 进程宕机
    MQ_CONGESTION = "mq_congestion"     # 消息队列堆积
    SLOW_QUERY = "slow_query"           # 慢查询
    REPLICATION_LAG = "replication_lag"  # 主从延迟
    UNKNOWN = "unknown"                 # 无法识别


# LLM 可选的告警类型列表（传入 prompt）
ALERT_TYPE_OPTIONS = ", ".join(t.value for t in AlertType)


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
    knowledge_query: str
    raw_service_info: str


class FaultPlanner:
    """
    故障规划器 - 使用 LLM 意图识别解析故障上下文并制定分析计划

    核心流程：
    1. 预加载 service_node/*.md 构建候选服务列表
    2. 调用 LLM 做结构化意图识别（服务名 + 告警类型 + 检索 query）
    3. 匹配服务拓扑，构建完整分析计划
    """

    def __init__(self):
        self._service_nodes_cache: Dict[str, ServiceNode] = {}
        self._load_all_service_nodes()
        self._llm = None  # 延迟初始化

    def _get_llm(self):
        """延迟获取 LLM 实例"""
        if self._llm is None:
            from llm.model import get_llm
            self._llm = get_llm()
        return self._llm

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

    def _build_service_candidates(self) -> str:
        """构建候选服务列表文本，供 LLM 选择"""
        if not self._service_nodes_cache:
            return "（暂无已注册服务）"

        lines = []
        for name, node in self._service_nodes_cache.items():
            desc = node.description[:80] if node.description else "无描述"
            deps = ", ".join(node.dependencies[:5]) if node.dependencies else "无"
            lines.append(f"- {name}: {desc}（依赖: {deps}）")
        return "\n".join(lines)

    def _llm_infer(self, fault_description: str) -> Dict[str, str]:
        """
        使用 LLM 进行故障意图识别

        一次调用同时输出：
        - service_name: 受影响的服务名称
        - alert_type: 告警类型
        - knowledge_query: 优化后的知识库检索 query

        Returns:
            解析后的 JSON dict，失败时返回默认值
        """
        candidates = self._build_service_candidates()

        prompt = f"""你是一个运维故障分析专家。请根据故障描述，从候选服务列表中识别最可能受影响的服务，判断告警类型，并生成知识库检索关键词。

## 故障描述
{fault_description}

## 候选服务列表
{candidates}

## 可选告警类型
{ALERT_TYPE_OPTIONS}

## 输出要求
请严格以 JSON 格式返回，不要包含其他内容：
```json
{{
    "service_name": "从候选列表中选择的服务名，如果都不匹配则填 unknown",
    "alert_type": "从可选告警类型中选择最匹配的一个",
    "knowledge_query": "用于知识库语义检索的优化 query，提取故障核心关键词，如：订单服务 数据库连接超时 HikariPool"
}}
```"""

        try:
            llm = self._get_llm()
            response = llm.invoke(prompt)
            content = response.content if hasattr(response, "content") else str(response)

            # 提取 JSON（兼容 ```json ... ``` 包裹和纯 JSON）
            json_match = re.search(r"\{[^{}]*\}", content, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
                logger.info(
                    f"[FaultPlanner] LLM 意图识别结果: "
                    f"service={result.get('service_name')} "
                    f"alert_type={result.get('alert_type')}"
                )
                return result

            logger.warning(f"[FaultPlanner] LLM 返回未包含有效 JSON: {content[:200]}")
        except Exception as e:
            logger.error(f"[FaultPlanner] LLM 意图识别失败: {e}")

        # 降级：返回默认值
        return {
            "service_name": "unknown",
            "alert_type": "unknown",
            "knowledge_query": fault_description,
        }

    def _parse_alert_type(self, alert_type_str: str) -> AlertType:
        """将字符串转换为 AlertType 枚举，无效值返回 UNKNOWN"""
        try:
            return AlertType(alert_type_str.lower())
        except ValueError:
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
        根据故障描述创建分析计划（LLM 意图识别）

        流程：
        1. 调用 LLM 识别 service_name + alert_type + knowledge_query
        2. 根据 service_name 加载服务拓扑
        3. 构建完整分析计划

        Args:
            fault_id: 故障唯一 ID
            fault_description: 故障描述文本

        Returns:
            FaultAnalysisPlan: 包含服务拓扑信息和 RAG 检索 query 的分析计划
        """
        # LLM 意图识别
        infer_result = self._llm_infer(fault_description)

        service_name = infer_result.get("service_name", "unknown")
        alert_type = self._parse_alert_type(infer_result.get("alert_type", "unknown"))
        knowledge_query = infer_result.get("knowledge_query", fault_description)

        # 加载服务拓扑
        service_node = self.get_service_node(service_name)
        service_info = self.format_service_info(service_node)

        logger.info(
            f"[FaultPlanner] 已创建故障分析计划 "
            f"fault_id={fault_id} service={service_name} "
            f"alert_type={alert_type.value}"
        )

        return FaultAnalysisPlan(
            fault_id=fault_id,
            fault_description=fault_description,
            service_name=service_name,
            alert_type=alert_type,
            service_node=service_node,
            knowledge_query=knowledge_query,
            raw_service_info=service_info,
        )
