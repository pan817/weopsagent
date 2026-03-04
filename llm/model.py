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
        # 支持流式输出
        streaming=False,
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


def get_streaming_llm() -> ChatOpenAI:
    """获取支持流式输出的 LLM 实例"""
    return ChatOpenAI(
        model=settings.openai_model,
        api_key=settings.openai_api_key,
        base_url=settings.openai_api_base,
        temperature=settings.openai_temperature,
        max_tokens=settings.openai_max_tokens,
        streaming=True,
    )
