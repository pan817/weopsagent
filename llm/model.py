"""
LLM 模块 - 封装 LangChain LLM 初始化逻辑，支持 OpenAI 兼容接口
"""
from functools import lru_cache
from typing import List, Optional, Sequence

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI

from config.settings import settings


@lru_cache(maxsize=1)
def get_llm() -> ChatOpenAI:
    """
    获取 LLM 实例（单例缓存）

    Returns:
        ChatOpenAI: 配置好的 LLM 实例
    """
    return ChatOpenAI(
        model=settings.openai_model,
        api_key=settings.openai_api_key,
        base_url=settings.openai_api_base,
        temperature=settings.openai_temperature,
        max_tokens=settings.openai_max_tokens,
        streaming=False,
        # 单次请求超时（秒），防止 LLM 服务端挂起导致长时间阻塞
        request_timeout=settings.openai_request_timeout,
        # 超时后最多重试次数
        max_retries=settings.openai_max_retries,
    )


def get_llm_with_tools(tools: Sequence[BaseTool]) -> BaseChatModel:
    """
    获取绑定了工具的 LLM 实例

    Args:
        tools: 要绑定的工具列表

    Returns:
        绑定了工具的 LLM 实例
    """
    llm = get_llm()
    return llm.bind_tools(tools)


@lru_cache(maxsize=1)
def get_sequential_llm() -> ChatOpenAI:
    """获取禁用并行工具调用的 LLM 实例（用于 ReAct 串行循环的 sub-agent）

    Qwen3 在并行 tool_calls 场景下，若消息历史中存在未完整收尾的 tool_call_id，
    会返回 400 错误。通过 model_kwargs 传递 parallel_tool_calls=False，
    强制模型每轮只返回一个工具调用，与 Thought→Action→Observation 模式完全匹配。
    """
    return ChatOpenAI(
        model=settings.openai_model,
        api_key=settings.openai_api_key,
        base_url=settings.openai_api_base,
        temperature=settings.openai_temperature,
        max_tokens=settings.openai_max_tokens,
        streaming=False,
        request_timeout=settings.openai_request_timeout,
        max_retries=settings.openai_max_retries,
        model_kwargs={"parallel_tool_calls": False},
    )


def get_streaming_llm() -> ChatOpenAI:
    """获取支持流式输出的 LLM 实例"""
    return ChatOpenAI(
        model=settings.openai_model,
        api_key=settings.openai_api_key,
        base_url=settings.openai_api_base,
        temperature=settings.openai_temperature,
        max_tokens=settings.openai_max_tokens,
        streaming=True,
        request_timeout=settings.openai_request_timeout,
        max_retries=settings.openai_max_retries,
    )
