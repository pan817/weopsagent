"""
基础工具类 - 提供重试、安全控制等通用机制
"""
import functools
import logging
import time
from typing import Any, Callable, Optional, TypeVar

from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
)

from config.settings import settings

logger = logging.getLogger(__name__)

T = TypeVar("T")


def with_retry(
    max_attempts: int = None,
    wait_seconds: float = None,
    exceptions: tuple = (Exception,),
):
    """
    重试装饰器工厂

    Args:
        max_attempts: 最大重试次数
        wait_seconds: 重试等待时间（秒）
        exceptions: 需要重试的异常类型

    Returns:
        装饰器
    """
    max_attempts = max_attempts or settings.tool_max_retries
    wait_seconds = wait_seconds or settings.tool_retry_wait_seconds

    return retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_fixed(wait_seconds),
        retry=retry_if_exception_type(exceptions),
        reraise=True,
    )


def safe_execute(func: Callable[..., T]) -> Callable[..., Optional[T]]:
    """
    安全执行装饰器 - 捕获所有异常并返回错误信息

    Args:
        func: 要包装的函数

    Returns:
        包装后的函数，异常时返回错误字符串而不是抛出
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs) -> Any:
        try:
            return func(*args, **kwargs)
        except PermissionError:
            # 人工确认被拒绝，向上传播
            raise
        except RetryError as e:
            error_msg = f"[{func.__name__}] 重试次数耗尽: {e.last_attempt.exception()}"
            logger.error(error_msg)
            return {"error": error_msg, "status": "failed"}
        except Exception as e:
            error_msg = f"[{func.__name__}] 执行失败: {type(e).__name__}: {e}"
            logger.error(error_msg, exc_info=True)
            return {"error": error_msg, "status": "failed"}
    return wrapper


def format_tool_result(
    tool_name: str,
    success: bool,
    data: Any = None,
    error: str = None,
    elapsed: float = None,
) -> dict:
    """
    格式化工具执行结果为统一结构

    Args:
        tool_name: 工具名称
        success: 是否成功
        data: 返回数据
        error: 错误信息
        elapsed: 耗时（秒）

    Returns:
        统一格式的结果字典
    """
    result = {
        "tool": tool_name,
        "success": success,
        "timestamp": time.time(),
    }
    if data is not None:
        result["data"] = data
    if error:
        result["error"] = error
    if elapsed is not None:
        result["elapsed_seconds"] = round(elapsed, 2)
    return result
