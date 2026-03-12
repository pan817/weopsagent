"""
滑动窗口记忆中间件 - 零成本短期记忆压缩

基于 LangChain 1.2.x 的 AgentMiddleware 机制实现，
通过 before_model hook 在每次 LLM 调用前裁剪对话历史，
只保留「第一条用户输入」+「最近 K 条消息」，直接丢弃中间消息。

与 SummarizationMiddleware 的区别：
┌───────────────────────┬──────────────────────┬─────────────────────┐
│                       │ SummarizationMiddleware │ SlidingWindowMiddleware │
├───────────────────────┼──────────────────────┼─────────────────────┤
│ 压缩方式              │ 调用 LLM 生成摘要      │ 直接丢弃中间消息      │
│ 额外 LLM 开销         │ 有（每次压缩一次调用）  │ 无                    │
│ 信息保留度            │ 高（摘要保留关键信息）  │ 低（中间信息全部丢失） │
│ 适用场景              │ 多轮复杂对话           │ 短任务/成本敏感场景    │
│ 延迟                  │ 压缩时有额外延迟       │ 零延迟                │
└───────────────────────┴──────────────────────┴─────────────────────┘

保留策略：
  [SystemMessage(s)] + [第一条HumanMessage] + [最近 K 条消息]

第一条用户输入通常包含完整的故障描述、服务拓扑、知识库上下文等关键信息，
丢失后 Agent 会失去任务目标，因此必须始终保留。

使用方式：
    middleware = SlidingWindowMiddleware(
        max_messages=20,       # 消息数超过 20 条触发裁剪
        preserve_recent=6,     # 保留最近 6 条消息
        preserve_first=True,   # 保留第一条用户输入（默认 True）
    )
    agent = create_agent(..., middleware=[middleware])
"""
import logging
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import SystemMessage

logger = logging.getLogger(__name__)


class SlidingWindowMiddleware(AgentMiddleware):
    """
    滑动窗口记忆 AgentMiddleware（LangChain 1.2.x）

    通过 before_model hook 在每次 LLM 调用前检查消息数，
    超限时直接裁剪中间消息，只保留首条输入和最近 K 条。

    零 LLM 开销，适合成本敏感或短任务场景。
    """

    def __init__(
        self,
        max_messages: int = 20,
        preserve_recent: int = 6,
        preserve_first: bool = True,
    ):
        """
        Args:
            max_messages: 消息总数阈值，超过时触发裁剪
            preserve_recent: 保留最近 K 条消息（至少 2）
            preserve_first: 是否保留第一条非 System 消息（通常是用户的故障描述）
        """
        self.max_messages = max(max_messages, 4)
        self.preserve_recent = max(preserve_recent, 2)
        self.preserve_first = preserve_first
        self._trim_count: int = 0
        self._total_dropped: int = 0

    # ===== Agent 生命周期 =====

    def before_agent(self, state: Any, runtime: Any) -> Any:
        """Agent 循环开始时重置统计"""
        self._trim_count = 0
        self._total_dropped = 0
        return None

    def after_agent(self, state: Any, runtime: Any) -> Any:
        """Agent 循环结束时输出裁剪统计"""
        if self._trim_count > 0:
            logger.info(
                f"[SlidingWindow] 统计: "
                f"裁剪次数={self._trim_count} "
                f"累计丢弃消息数={self._total_dropped}"
            )
        return None

    # ===== LLM 调用前裁剪 =====

    def before_model(self, state: Any, runtime: Any) -> Any:
        """
        LLM 调用前检查消息数，超限时裁剪中间消息

        裁剪策略：
        1. 提取所有 SystemMessage（始终保留在最前）
        2. 提取第一条非 System 消息（preserve_first=True 时保留）
        3. 提取最近 preserve_recent 条消息
        4. 丢弃中间的所有消息
        5. 拼接：[System...] + [第一条输入] + [最近K条]
        """
        messages = getattr(state, "messages", None)
        if messages is None or not isinstance(messages, list):
            return None

        if len(messages) <= self.max_messages:
            return None

        new_messages = self._trim(messages)
        dropped = len(messages) - len(new_messages)

        if dropped > 0:
            state.messages = new_messages
            self._trim_count += 1
            self._total_dropped += dropped
            logger.info(
                f"[SlidingWindow] 裁剪完成: "
                f"{len(messages)} → {len(new_messages)} 条消息, "
                f"丢弃 {dropped} 条 "
                f"(第 {self._trim_count} 次裁剪)"
            )

        return None

    def _trim(self, messages: list) -> list:
        """
        执行消息裁剪

        Returns:
            裁剪后的消息列表
        """
        # 1. 分离 SystemMessage
        system_msgs = []
        non_system_msgs = []
        for msg in messages:
            if isinstance(msg, SystemMessage):
                system_msgs.append(msg)
            else:
                non_system_msgs.append(msg)

        # 非系统消息不够裁剪
        if len(non_system_msgs) <= self.preserve_recent + (1 if self.preserve_first else 0):
            return messages

        # 2. 提取第一条非 System 消息
        first_msg = None
        if self.preserve_first and non_system_msgs:
            first_msg = non_system_msgs[0]

        # 3. 提取最近 K 条消息
        recent_msgs = non_system_msgs[-self.preserve_recent:]

        # 4. 避免第一条消息和最近消息重复
        result = list(system_msgs)
        if first_msg is not None and first_msg not in recent_msgs:
            result.append(first_msg)
        result.extend(recent_msgs)

        return result
