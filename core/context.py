"""
请求上下文 - 基于 ContextVar 的 correlation_id 传播

correlation_id 在整个调用链（API → FaultAgent → 子 Agent → 工具）中自动传播，
无需手动传参。asyncio 任务和线程池任务均会继承调用方的上下文。

使用方式：
    from core.context import get_correlation_id, set_correlation_id, new_correlation_id

    # 设置（API 层或 CLI 入口）
    cid = new_correlation_id()

    # 读取（任意层级）
    cid = get_correlation_id()

日志格式需包含 %(correlation_id)s 并注册 CorrelationIdFilter。
"""
import logging
import uuid
from contextvars import ContextVar

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")


def get_correlation_id() -> str:
    """返回当前上下文的 correlation_id，未设置时返回空字符串"""
    return _correlation_id.get()


def set_correlation_id(cid: str) -> None:
    """设置当前上下文的 correlation_id"""
    _correlation_id.set(cid)


def new_correlation_id() -> str:
    """生成新的 correlation_id 并写入当前上下文，返回生成的 ID"""
    cid = uuid.uuid4().hex[:12]
    _correlation_id.set(cid)
    return cid


class CorrelationIdFilter(logging.Filter):
    """
    日志 Filter：自动将 correlation_id 注入每条 LogRecord。

    注册后，日志格式中可使用 %(correlation_id)s。
    未设置时输出 "-"，不影响其他日志字段。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id() or "-"
        return True
