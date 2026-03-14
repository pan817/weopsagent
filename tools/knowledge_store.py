"""
知识库存储工具 - 将有效故障处理措施存入长期记忆

使用 LangChain 1.2.x @tool 装饰器实现，不再继承 BaseTool 类。
"""
import json
import logging
import time

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from .base import format_tool_result

logger = logging.getLogger(__name__)


class StoreKnowledgeInput(BaseModel):
    """知识库存储工具输入参数"""
    title: str = Field(description="措施标题，简洁描述故障场景")
    fault_description: str = Field(description="故障现象描述")
    root_cause: str = Field(description="故障根因分析结论")
    solution: str = Field(description="故障处理方案和步骤")
    category: str = Field(
        default="history",
        description="措施类别: general（通用）, scenario（场景）, history（历史案例）"
    )
    tags: str = Field(
        default="",
        description="标签，逗号分隔，如 'redis,connection,timeout'"
    )
    effectiveness: str = Field(
        default="confirmed",
        description="有效性: confirmed（已确认有效）, partial（部分有效）"
    )
    service: str = Field(
        default="",
        description="故障所属服务名称，如 'order-service'，用于精确索引"
    )
    alert_type: str = Field(
        default="",
        description="告警类型，如 'db_connection'、'high_cpu'，用于热点缓存索引"
    )


@tool(
    "store_knowledge",
    args_schema=StoreKnowledgeInput,
    description="将有效的故障处理措施存入知识库，记录故障现象、根因和解决方案，供未来类似故障参考。",
)
def store_knowledge(
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
    start_time = time.time()
    try:
        # 延迟导入避免循环依赖
        from memory.long_term import LongTermMemory
        from memory.memory_manager import get_memory_manager

        # L3 ChromaDB 写入（同步，持久化兜底层）
        ltm = LongTermMemory()
        doc_id = ltm.store_fault_experience(
            title=title,
            fault_description=fault_description,
            root_cause=root_cause,
            solution=solution,
            category=category,
            tags=tags,
            effectiveness=effectiveness,
        )

        # L2/L1 异步写入（Judge 评分 → ES → Redis，不阻塞主流程）
        get_memory_manager().store_async({
            "doc_id": doc_id,
            "title": title,
            "fault_description": fault_description,
            "root_cause": root_cause,
            "solution": solution,
            "category": category,
            "tags": [t.strip() for t in tags.split(",") if t.strip()],
            "effectiveness": effectiveness,
            "service": service,
            "alert_type": alert_type,
        })

        elapsed = time.time() - start_time
        logger.info(f"[StoreKnowledge] 已存储处理措施到知识库: {title} (id={doc_id})")
        return json.dumps(
            format_tool_result(
                "store_knowledge",
                True,
                data={"doc_id": doc_id, "title": title, "category": category},
                elapsed=elapsed,
            ),
            ensure_ascii=False,
        )
    except Exception as e:
        elapsed = time.time() - start_time
        logger.error(f"[StoreKnowledge] 存储失败: {e}")
        return json.dumps(
            format_tool_result("store_knowledge", False, error=str(e), elapsed=elapsed),
            ensure_ascii=False,
        )
