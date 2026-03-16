"""
审计日志中间件 - 记录所有 Agent 操作的详细审计日志

基于 LangChain 1.2.x 的 AgentMiddleware 机制实现，
通过继承 AgentMiddleware 并重写 hook 方法拦截 Agent 执行各阶段。

可拦截的阶段：
- before_agent / after_agent：整个 Agent 循环的开始和结束
- before_model / after_model：每次 LLM 调用前后
- wrap_tool_call：工具调用（包装器，可记录完整输入输出）
"""
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

from config.settings import settings
from core.context import CorrelationIdFilter, get_correlation_id

# 审计日志使用独立的 logger
audit_logger = logging.getLogger("weops.audit")


def _setup_audit_logger():
    """配置审计日志处理器"""
    if audit_logger.handlers:
        return
    audit_logger.setLevel(logging.INFO)
    audit_logger.addFilter(CorrelationIdFilter())

    log_path = Path(settings.audit_log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.INFO)

    formatter = logging.Formatter(
        fmt='%(asctime)s | %(levelname)s | [%(correlation_id)s] | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )
    file_handler.setFormatter(formatter)
    audit_logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(formatter)
    audit_logger.addHandler(console_handler)


_setup_audit_logger()


class AuditLogMiddleware(AgentMiddleware):
    """
    审计日志 AgentMiddleware（LangChain 1.2.x）

    通过 LangChain 1.2.x 原生 Middleware 机制，
    在 Agent 执行各阶段记录详细审计日志：

    - before_agent：记录故障处理开始，含输入消息摘要
    - after_agent：记录故障处理结束，含耗时和输出摘要
    - before_model：记录每次 LLM 调用开始
    - after_model：记录每次 LLM 调用完成
    - wrap_tool_call：包装工具调用，记录完整输入/输出/耗时/异常
    """

    def __init__(self, fault_id: Optional[str] = None):
        """
        Args:
            fault_id: 关联的故障 ID，注入到所有日志记录中。
                      不提供时，运行时会尝试从 RunnableConfig.configurable["fault_id"] 读取。
        """
        self._init_fault_id = fault_id  # 构造时传入的静态 fault_id
        self.fault_id = fault_id or "UNKNOWN"
        self._agent_start_time: Optional[float] = None
        self._model_call_count: int = 0

    def _log(self, level: str, event: str, **kwargs):
        """统一结构化日志输出"""
        record = {
            "correlation_id": get_correlation_id() or "-",
            "fault_id": self.fault_id,
            "event": event,
            **kwargs,
        }
        msg = json.dumps(record, ensure_ascii=False, default=str)
        getattr(audit_logger, level.lower())(msg)

    # ===== Agent 生命周期 =====

    def before_agent(self, state: Any, runtime: Any) -> Any:
        """Agent 循环开始 - 记录故障处理启动"""
        self._agent_start_time = time.time()
        self._model_call_count = 0

        # 若构造时未传 fault_id，尝试从 RunnableConfig.configurable 读取
        # 支持 Subagent 模式下的单例 Agent 复用（不同调用传不同 fault_id）
        if not self._init_fault_id:
            try:
                config = getattr(runtime, "config", None)
                configurable = getattr(config, "configurable", None) or {}
                runtime_fault_id = configurable.get("fault_id")
                if runtime_fault_id:
                    self.fault_id = runtime_fault_id
            except Exception:
                pass

        messages = getattr(state, "messages", []) or []
        last_msg_preview = ""
        if messages:
            content = getattr(messages[-1], "content", str(messages[-1]))
            last_msg_preview = str(content)[:300]

        self._log("info", "agent_start",
                  message_count=len(messages),
                  last_message_preview=last_msg_preview)
        return None

    def after_agent(self, state: Any, runtime: Any) -> Any:
        """Agent 循环结束 - 记录故障处理完成"""
        elapsed = time.time() - (self._agent_start_time or time.time())
        messages = getattr(state, "messages", []) or []

        output_preview = ""
        for msg in reversed(messages):
            role = type(msg).__name__
            if "AI" in role or "Assistant" in role:
                content = getattr(msg, "content", "")
                if isinstance(content, str) and content:
                    output_preview = content[:300]
                    break

        self._log("info", "agent_end",
                  elapsed_seconds=round(elapsed, 2),
                  total_messages=len(messages),
                  model_calls=self._model_call_count,
                  output_preview=output_preview)
        return None

    # ===== LLM 调用 =====

    def before_model(self, state: Any, runtime: Any) -> Any:
        """LLM 调用开始 - 记录调用序号和消息数"""
        self._model_call_count += 1
        messages = getattr(state, "messages", []) or []
        self._log("info", "model_start",
                  call_index=self._model_call_count,
                  message_count=len(messages))
        return None

    def after_model(self, state: Any, runtime: Any) -> Any:
        """LLM 调用结束 - 记录最新 AI 响应内容"""
        messages = getattr(state, "messages", []) or []
        latest_ai_preview = ""
        if messages:
            last = messages[-1]
            content = getattr(last, "content", "")
            if isinstance(content, str) and content:
                latest_ai_preview = content[:300]
            elif isinstance(content, list):
                texts = [
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                latest_ai_preview = "\n".join(texts)[:300]

        self._log("info", "model_end",
                  call_index=self._model_call_count,
                  ai_response_preview=latest_ai_preview)
        return None

    # ===== 工具调用（包装器）=====

    def wrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        """
        工具调用包装器 - 记录完整的工具调用输入、输出和耗时

        Args:
            request: ToolCallRequest，包含 tool_call 字典和 BaseTool 实例
            handler: 执行工具的回调函数

        Returns:
            ToolMessage 或 Command
        """
        tool_call = getattr(request, "tool_call", {}) or {}
        tool_name = tool_call.get("name", "unknown")
        tool_args = tool_call.get("args", {})
        tool_id = tool_call.get("id", "")

        self._log("info", "tool_start",
                  tool_name=tool_name,
                  tool_id=tool_id,
                  tool_args_preview=str(tool_args)[:300])

        start_time = time.time()
        try:
            result = handler(request)
            elapsed = time.time() - start_time

            result_preview = (
                str(result.content)[:300]
                if isinstance(result, ToolMessage)
                else str(result)[:300]
            )

            self._log("info", "tool_end",
                      tool_name=tool_name,
                      tool_id=tool_id,
                      elapsed_seconds=round(elapsed, 2),
                      result_preview=result_preview)
            return result

        except Exception as e:
            elapsed = time.time() - start_time
            self._log("error", "tool_error",
                      tool_name=tool_name,
                      tool_id=tool_id,
                      elapsed_seconds=round(elapsed, 2),
                      error_type=type(e).__name__,
                      error_message=str(e))
            raise
