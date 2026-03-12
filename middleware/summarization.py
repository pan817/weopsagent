"""
对话摘要中间件 - 当对话历史过长时自动压缩早期消息

基于 LangChain 1.2.x 的 AgentMiddleware 机制实现，
通过 before_model hook 在每次 LLM 调用前检查对话历史长度，
当消息数或 Token 数超过阈值时，调用 LLM 将早期消息压缩为一条摘要，
保留最近的消息不动，从而控制 context window 消耗。

典型场景：
- 主 Agent 多轮对话：故障处理过程中反复调用子 Agent，消息快速膨胀
- 子 Agent 密集工具调用：MonitorAgent 可能一次调用 5+ 个监控工具
- 长时间运行会话：多轮 continue_conversation 导致历史无限增长

使用方式：
    middleware = SummarizationMiddleware(
        max_messages=20,         # 消息数超过 20 条触发压缩
        max_tokens=8000,         # Token 估算超过 8000 触发压缩
        preserve_recent=6,       # 保留最近 6 条消息不压缩
        summary_model=None,      # 用当前 Agent 的模型做摘要（也可指定低成本模型）
    )
    agent = create_agent(..., middleware=[middleware])
"""
import logging
import time
from typing import Any, Callable, List, Optional

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

logger = logging.getLogger(__name__)

# 摘要 Prompt 模板
_SUMMARIZE_PROMPT = """请将以下对话历史压缩为一段简洁的摘要。

要求：
1. 保留关键事实：故障描述、已执行的操作、监控采集结果、分析结论、恢复状态
2. 保留重要的数据点：服务名称、主机 IP、错误信息、关键指标数值
3. 丢弃冗余信息：重复的工具调用参数、中间推理过程、格式化模板
4. 使用中文输出，保持专业运维语言风格
5. 控制在 500 字以内

## 需要压缩的对话历史

{conversation}"""


def _estimate_tokens(text: str) -> int:
    """
    粗略估算文本 Token 数

    使用 1 个中文字符 ≈ 1.5 token、1 个英文单词 ≈ 1.3 token 的经验值。
    精确计算需要 tiktoken，这里用字符数近似以避免额外依赖。
    """
    if not text:
        return 0
    # 粗略估算：中文每字约 1.5 token，英文每 4 字符约 1 token
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    other_chars = len(text) - chinese_chars
    return int(chinese_chars * 1.5 + other_chars * 0.3)


def _messages_to_text(messages: list) -> str:
    """将消息列表转为可读文本（供摘要 LLM 阅读）"""
    lines = []
    for msg in messages:
        role = type(msg).__name__.replace("Message", "")
        content = getattr(msg, "content", "")
        if isinstance(content, list):
            texts = [
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            content = "\n".join(texts)
        if not content:
            continue
        # 截断过长的单条消息
        if len(content) > 2000:
            content = content[:1800] + "\n...(已截断)"
        lines.append(f"[{role}] {content}")
    return "\n\n".join(lines)


class SummarizationMiddleware(AgentMiddleware):
    """
    对话摘要 AgentMiddleware（LangChain 1.2.x）

    通过 before_model hook 在每次 LLM 调用前检查对话历史，
    当消息数或估算 Token 数超过阈值时，自动将早期消息压缩为摘要。

    压缩策略：
    - 保留 SystemMessage（始终在最前）
    - 保留最近 preserve_recent 条消息不动
    - 将中间的消息调用 LLM 压缩为一条 SystemMessage 摘要
    - 压缩后的消息列表：[SystemMessage, 摘要SystemMessage, ...最近N条消息]
    """

    def __init__(
        self,
        max_messages: int = 20,
        max_tokens: int = 8000,
        preserve_recent: int = 6,
        summary_model: Optional[Any] = None,
    ):
        """
        Args:
            max_messages: 消息数阈值，超过时触发压缩
            max_tokens: Token 估算阈值，超过时触发压缩
            preserve_recent: 保留最近 N 条消息不压缩（至少 2）
            summary_model: 用于生成摘要的 LLM 实例（None 则使用当前 Agent 的模型）
        """
        self.max_messages = max_messages
        self.max_tokens = max_tokens
        self.preserve_recent = max(preserve_recent, 2)
        self._summary_model = summary_model
        self._summarize_count: int = 0
        self._total_compressed_messages: int = 0

    # ===== Agent 生命周期 =====

    def before_agent(self, state: Any, runtime: Any) -> Any:
        """Agent 循环开始时重置统计"""
        self._summarize_count = 0
        self._total_compressed_messages = 0
        return None

    def after_agent(self, state: Any, runtime: Any) -> Any:
        """Agent 循环结束时输出压缩统计"""
        if self._summarize_count > 0:
            logger.info(
                f"[Summarization] 统计: "
                f"压缩次数={self._summarize_count} "
                f"累计压缩消息数={self._total_compressed_messages}"
            )
        return None

    # ===== LLM 调用前检查 =====

    def before_model(self, state: Any, runtime: Any) -> Any:
        """
        LLM 调用前检查对话历史长度，超限时自动压缩

        通过修改 state.messages 实现就地压缩（LangChain 1.2.x 允许在
        before_model 中修改 state）。
        """
        messages = getattr(state, "messages", None)
        if messages is None or not isinstance(messages, list):
            return None

        if not self._should_summarize(messages):
            return None

        # 执行压缩
        try:
            model = self._summary_model or getattr(runtime, "model", None)
            if model is None:
                logger.warning("[Summarization] 无可用模型，跳过压缩")
                return None

            new_messages = self._do_summarize(messages, model)
            # 就地替换消息列表
            state.messages = new_messages

            self._summarize_count += 1
            logger.info(
                f"[Summarization] 压缩完成: "
                f"{len(messages)} → {len(new_messages)} 条消息 "
                f"(第 {self._summarize_count} 次压缩)"
            )
        except Exception as e:
            logger.error(f"[Summarization] 压缩失败（保持原消息）: {e}")

        return None

    def _should_summarize(self, messages: list) -> bool:
        """判断是否需要触发压缩"""
        # 条件 1：消息数超限
        if len(messages) > self.max_messages:
            return True

        # 条件 2：Token 估算超限
        total_tokens = 0
        for msg in messages:
            content = getattr(msg, "content", "")
            if isinstance(content, list):
                content = str(content)
            total_tokens += _estimate_tokens(str(content))
            if total_tokens > self.max_tokens:
                return True

        return False

    def _do_summarize(self, messages: list, model: Any) -> list:
        """
        执行对话压缩

        策略：
        1. 分离出 SystemMessage（保留在最前）
        2. 将非 System 消息分为「待压缩」和「保留」两部分
        3. 调用 LLM 对「待压缩」部分生成摘要
        4. 拼接：[原始System, 摘要System, 保留消息...]
        """
        # 分离 SystemMessage
        system_messages = []
        non_system_messages = []
        for msg in messages:
            if isinstance(msg, SystemMessage):
                system_messages.append(msg)
            else:
                non_system_messages.append(msg)

        # 如果非系统消息不够多，不压缩
        if len(non_system_messages) <= self.preserve_recent:
            return messages

        # 分割：待压缩 vs 保留
        cutoff = len(non_system_messages) - self.preserve_recent
        to_compress = non_system_messages[:cutoff]
        to_preserve = non_system_messages[cutoff:]

        self._total_compressed_messages += len(to_compress)

        # 调用 LLM 生成摘要
        conversation_text = _messages_to_text(to_compress)
        summary_prompt = _SUMMARIZE_PROMPT.format(conversation=conversation_text)

        start_time = time.time()
        response = model.invoke(summary_prompt)
        elapsed = time.time() - start_time

        summary_content = response.content if hasattr(response, "content") else str(response)

        logger.debug(
            f"[Summarization] 摘要生成耗时 {elapsed:.2f}s, "
            f"压缩 {len(to_compress)} 条消息"
        )

        # 构造摘要 SystemMessage
        summary_msg = SystemMessage(
            content=f"[对话历史摘要 - 已压缩 {len(to_compress)} 条早期消息]\n\n{summary_content}"
        )

        # 拼接最终消息列表
        return system_messages + [summary_msg] + to_preserve
