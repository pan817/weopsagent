"""
恢复 Subagent - 故障恢复操作执行

Subagent 设计要点：
- 编译后的 Agent 实例按 console_confirm_mode 分别缓存（最多 2 个实例）
  True  → 控制台交互确认（开发/测试环境）
  False → 外部 Webhook 确认（生产 API 模式）
- Prompt 从 agents/prompts/recovery_agent.txt 加载，便于维护
- HumanConfirmMiddleware 在危险工具调用前自动触发人工确认
- AuditLogMiddleware 不绑定静态 fault_id，由 RunnableConfig.configurable 动态注入

使用工具：restart_service（⚠️危险）、store_knowledge（安全）
"""
import logging
import re
from pathlib import Path
from typing import Any, Dict

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver

from llm.model import get_llm
from middleware.audit_log import AuditLogMiddleware
from middleware.human_confirm import HumanConfirmMiddleware
from tools import get_tool_registry

logger = logging.getLogger(__name__)

# 恢复 Subagent 工具集（含危险工具，需人工确认中间件把关）
RECOVERY_TOOLS = get_tool_registry().get_group("recovery")

# ===== 单例缓存（按 console_confirm_mode 分别缓存）=====
# console_confirm_mode=True  → 开发/测试环境，控制台交互确认
# console_confirm_mode=False → 生产环境，外部 API/Webhook 确认
_agent_cache: Dict[bool, Any] = {}


def _load_prompt() -> str:
    """从 agents/prompts/recovery_agent.txt 加载 System Prompt"""
    prompt_path = Path(__file__).parent / "prompts" / "recovery_agent.txt"
    return prompt_path.read_text(encoding="utf-8").strip()


def _get_agent(console_confirm_mode: bool = True) -> Any:
    """
    获取 Recovery Subagent 编译实例（进程级单例，按确认模式分别缓存）

    console_confirm_mode 影响 HumanConfirmMiddleware 的行为，
    因此不同模式需要各自独立的编译实例。
    fault_id 通过 RunnableConfig.configurable["fault_id"] 在调用时动态注入。
    """
    if console_confirm_mode not in _agent_cache:
        logger.info(
            f"[RecoveryAgent] 编译 Recovery Subagent "
            f"console_confirm_mode={console_confirm_mode}（首次初始化）"
        )
        _agent_cache[console_confirm_mode] = create_agent(
            model=get_llm(),
            tools=RECOVERY_TOOLS,
            system_prompt=_load_prompt(),
            middleware=[
                AuditLogMiddleware(),                            # fault_id 由运行时动态注入
                HumanConfirmMiddleware(console_mode=console_confirm_mode),
            ],
            checkpointer=InMemorySaver(),
            name="recovery_agent",
        )
    return _agent_cache[console_confirm_mode]


def run_recovery_node(
    state: Dict[str, Any],
    config: RunnableConfig,
    console_confirm_mode: bool = True,
) -> Dict[str, Any]:
    """
    恢复节点（LangGraph 节点函数）

    读取 analysis_result 和 recovery_plan，调用 Recovery Subagent 执行恢复操作，
    将结果写回 state["recovery_actions"] 和 state["is_resolved"]。
    """
    fault_id = state.get("fault_id", "UNKNOWN")
    fault_description = state.get("fault_description", "")
    analysis_result = state.get("analysis_result", "")
    recovery_plan = state.get("recovery_plan", "")
    root_cause = state.get("root_cause", "")
    service_name = state.get("service_name", "unknown")

    logger.info(f"[RecoveryAgent] 开始执行恢复 fault_id={fault_id}")

    prompt = f"""## 故障信息
- 服务名称：{service_name}
- 故障描述：{fault_description}

## 根因分析
{root_cause}

## 恢复方案
{recovery_plan}

## 完整分析结果（参考）
{analysis_result}

请按照恢复方案执行操作。注意：
1. 重启服务等危险操作需要人工确认，系统会自动暂停等待
2. 操作完成后请调用 store_knowledge 工具保存本次处理经验
3. 最后给出明确的恢复状态结论（RESOLVED/PARTIAL/FAILED）"""

    try:
        agent = _get_agent(console_confirm_mode)
        result = agent.invoke(
            {"messages": [HumanMessage(content=prompt)]},
            config=RunnableConfig(
                configurable={
                    "thread_id": f"{fault_id}-recovery",
                    "fault_id": fault_id,   # 供 AuditLogMiddleware 动态读取
                },
            ),
        )
        messages = result.get("messages", [])
        recovery_text = _extract_last_text(messages)
        is_resolved = _check_resolved(recovery_text)

        logger.info(
            f"[RecoveryAgent] 恢复完成 fault_id={fault_id} is_resolved={is_resolved}"
        )

        return {
            "recovery_actions": recovery_text,
            "is_resolved": is_resolved,
            "messages": messages[-1:] if messages else [],
        }

    except PermissionError as e:
        # 危险操作被人工拒绝
        logger.warning(f"[RecoveryAgent] 危险操作被拒绝 fault_id={fault_id}: {e}")
        return {
            "recovery_actions": f"操作被拒绝：{e}",
            "is_resolved": False,
            "error_message": f"操作被拒绝: {e}",
        }
    except Exception as e:
        logger.error(f"[RecoveryAgent] 执行恢复失败 fault_id={fault_id}: {e}")
        return {
            "recovery_actions": f"恢复执行失败: {e}",
            "is_resolved": False,
            "error_message": f"RecoveryAgent 异常: {e}",
        }


def _extract_last_text(messages) -> str:
    """从消息列表提取最后一条 AI 文本响应"""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and msg.content:
            if isinstance(msg.content, str):
                return msg.content
            elif isinstance(msg.content, list):
                texts = [
                    p.get("text", "") for p in msg.content
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                return "\n".join(texts)
    return "（恢复 Agent 未返回有效结果）"


def _check_resolved(recovery_text: str) -> bool:
    """
    从 RecoveryAgent 输出文本中判断故障是否已恢复。

    解析策略（优先级从高到低）：
    1. 结构化标记解析（主路径）：匹配 prompt 中约定的 `RECOVERY_STATUS: RESOLVED/PARTIAL/FAILED`
       只有明确为 RESOLVED 才返回 True，PARTIAL 和 FAILED 均返回 False
    2. 关键词匹配（降级路径）：LLM 未输出机器可读标记时的兜底
       仅在确认无否定语境时才判为已恢复
    """
    # ── 主路径：解析机器可读状态行 ──────────────────────────────────────────────
    status_match = re.search(
        r"RECOVERY_STATUS\s*:\s*(RESOLVED|PARTIAL|FAILED)",
        recovery_text,
        re.IGNORECASE,
    )
    if status_match:
        status = status_match.group(1).upper()
        resolved = status == "RESOLVED"
        logger.info(f"[RecoveryAgent] 解析到机器可读状态: {status} → is_resolved={resolved}")
        return resolved

    # ── 降级路径：关键词匹配 ─────────────────────────────────────────────────────
    logger.warning(
        "[RecoveryAgent] 未找到 RECOVERY_STATUS 机器可读标记，降级为关键词匹配。"
        "请检查 recovery_agent.txt 输出格式约束。"
    )
    text_lower = recovery_text.lower()
    # 明确排除 PARTIAL / FAILED 关键词优先
    if any(kw in text_lower for kw in ["partial", "failed", "未恢复", "恢复失败", "需人工"]):
        return False
    # 有明确恢复词且无否定前缀
    resolved_keywords = ["resolved", "已恢复", "恢复成功", "故障已消除"]
    for kw in resolved_keywords:
        if kw in text_lower:
            # 检查否定前缀：未/没有/尚未/仍未
            negative_prefix = any(
                neg + kw in text_lower
                for neg in ["未", "没有", "尚未", "仍未"]
            )
            if not negative_prefix:
                return True
    return False
