"""
人工确认中间件 - 在执行危险操作前请求人工授权

基于 LangChain 1.2.x 的 AgentMiddleware 机制实现，
通过重写 wrap_tool_call hook 拦截危险工具的执行，
暂停等待人工确认后再继续执行。

支持三种确认模式：
1. 控制台交互（开发/调试模式）
2. Webhook 回调（生产模式）
3. 未配置时默认拒绝（安全第一）
"""
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Optional

import httpx
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

from config.settings import settings

logger = logging.getLogger(__name__)


class ConfirmStatus(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    TIMEOUT = "TIMEOUT"
    MODIFIED = "MODIFIED"


@dataclass
class ConfirmationResult:
    """人工确认结果"""
    status: ConfirmStatus
    operation_id: str
    operator: Optional[str] = None
    comment: Optional[str] = None
    modified_params: Optional[Dict[str, Any]] = None
    timestamp: float = field(default_factory=time.time)

    @property
    def is_approved(self) -> bool:
        return self.status == ConfirmStatus.APPROVED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "operation_id": self.operation_id,
            "operator": self.operator,
            "comment": self.comment,
            "modified_params": self.modified_params,
            "timestamp": self.timestamp,
        }


# 危险工具列表 - 这些工具执行前必须通过人工确认
DANGEROUS_TOOLS = {
    "restart_service",
    "kill_process",
    "execute_sql",
    "flush_redis",
    "purge_mq_queue",
}


class HumanConfirmMiddleware(AgentMiddleware):
    """
    人工确认 AgentMiddleware（LangChain 1.2.x）

    通过重写 wrap_tool_call 拦截危险工具执行：
    - 检测到危险工具时，暂停执行等待人工授权
    - 获得授权后继续执行 handler(request)
    - 被拒绝时抛出 PermissionError，终止执行

    此 middleware 在 create_agent(middleware=[...]) 中注入，
    无需通过 RunnableConfig(callbacks=[...]) 传递。
    """

    def __init__(
        self,
        console_mode: bool = True,
        webhook_url: Optional[str] = None,
        timeout: int = None,
    ):
        """
        Args:
            console_mode: 是否使用控制台交互确认（开发模式）
            webhook_url: 发送确认请求的 Webhook URL（生产模式）
            timeout: 等待确认的超时时间（秒）
        """
        self.console_mode = console_mode
        self.webhook_url = webhook_url or settings.human_confirm_webhook_url
        self.timeout = timeout or settings.human_confirm_timeout
        # 用于存储外部提交的确认结果（API 接口写入）
        self._confirmation_results: Dict[str, ConfirmationResult] = {}

    # ===== 工具调用拦截 =====

    def wrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        """
        拦截所有工具调用，对危险工具请求人工确认

        Args:
            request: ToolCallRequest，包含 tool_call 字典
            handler: 实际执行工具的回调，仅授权后调用

        Returns:
            ToolMessage 或 Command

        Raises:
            PermissionError: 当危险操作被人工拒绝或超时时
        """
        tool_call = getattr(request, "tool_call", {}) or {}
        tool_name = tool_call.get("name", "")
        tool_args = tool_call.get("args", {})
        tool_id = tool_call.get("id", str(time.time()))

        # 非危险工具直接放行
        if tool_name not in DANGEROUS_TOOLS:
            return handler(request)

        logger.warning(
            f"[HumanConfirm] 检测到危险操作: {tool_name}，等待人工确认..."
        )

        # 请求人工确认
        result = self._request_confirmation(
            operation_id=tool_id,
            tool_name=tool_name,
            tool_input=tool_args,
        )

        if not result.is_approved:
            raise PermissionError(
                f"危险操作 '{tool_name}' 被拒绝或超时。"
                f"状态: {result.status.value}，"
                f"操作员: {result.operator}，"
                f"备注: {result.comment}"
            )

        logger.info(
            f"[HumanConfirm] 操作已获授权: {tool_name}，"
            f"操作员: {result.operator}"
        )
        return handler(request)

    # ===== 内部确认逻辑 =====

    def _request_confirmation(
        self,
        operation_id: str,
        tool_name: str,
        tool_input: Dict[str, Any],
    ) -> ConfirmationResult:
        """路由到对应的确认方式"""
        # 优先检查是否已有外部提交的结果（API 模式）
        if operation_id in self._confirmation_results:
            return self._confirmation_results.pop(operation_id)

        if self.console_mode:
            return self._console_confirm(operation_id, tool_name, tool_input)
        elif self.webhook_url:
            return self._webhook_confirm(operation_id, tool_name, tool_input)
        else:
            # 未配置确认方式，安全拒绝
            logger.error("[HumanConfirm] 未配置确认方式，默认拒绝危险操作")
            return ConfirmationResult(
                status=ConfirmStatus.REJECTED,
                operation_id=operation_id,
                comment="未配置确认方式，自动拒绝",
            )

    def _console_confirm(
        self,
        operation_id: str,
        tool_name: str,
        tool_input: Dict[str, Any],
    ) -> ConfirmationResult:
        """控制台交互确认（开发/调试场景）"""
        print("\n" + "=" * 60)
        print("⚠️  危险操作确认请求")
        print("=" * 60)
        print(f"操作 ID  : {operation_id}")
        print(f"操作类型 : {tool_name}")
        print(f"操作参数 : {json.dumps(tool_input, ensure_ascii=False, indent=2)}")
        print("-" * 60)
        print(f"请在 {self.timeout} 秒内输入确认:")
        print("  输入 'y' 或 'yes'          → 确认执行")
        print("  输入 'n' 或 'no'           → 拒绝执行")
        print("  输入 'comment:<备注>'       → 添加备注并确认")
        print("=" * 60)

        start_time = time.time()
        operator = "console_user"

        while time.time() - start_time < self.timeout:
            try:
                import select
                import sys
                remaining = self.timeout - (time.time() - start_time)
                rlist, _, _ = select.select([sys.stdin], [], [], min(5.0, remaining))
                if not rlist:
                    continue
                user_input = sys.stdin.readline().strip().lower()
            except (OSError, ImportError):
                # Windows 或测试环境：直接阻塞读
                try:
                    user_input = input(f"确认操作 {tool_name}? (y/n): ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    break

            if user_input in ("y", "yes"):
                return ConfirmationResult(
                    status=ConfirmStatus.APPROVED,
                    operation_id=operation_id,
                    operator=operator,
                )
            elif user_input in ("n", "no"):
                return ConfirmationResult(
                    status=ConfirmStatus.REJECTED,
                    operation_id=operation_id,
                    operator=operator,
                    comment="用户拒绝",
                )
            elif user_input.startswith("comment:"):
                return ConfirmationResult(
                    status=ConfirmStatus.APPROVED,
                    operation_id=operation_id,
                    operator=operator,
                    comment=user_input[8:],
                )
            else:
                print("无效输入，请输入 y/yes/n/no 或 comment:<备注>")

        logger.warning(f"[HumanConfirm] 操作 {operation_id} 等待超时")
        return ConfirmationResult(
            status=ConfirmStatus.TIMEOUT,
            operation_id=operation_id,
            comment=f"等待超时（{self.timeout}秒）",
        )

    def _webhook_confirm(
        self,
        operation_id: str,
        tool_name: str,
        tool_input: Dict[str, Any],
    ) -> ConfirmationResult:
        """通过 Webhook 发送确认请求并轮询等待结果（生产场景）"""
        payload = {
            "operation_id": operation_id,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "timeout": self.timeout,
            "timestamp": time.time(),
        }

        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(self.webhook_url, json=payload)
                resp.raise_for_status()
                logger.info(f"[HumanConfirm] 已发送确认请求: {self.webhook_url}")
        except Exception as e:
            logger.error(f"[HumanConfirm] Webhook 发送失败: {e}")
            return ConfirmationResult(
                status=ConfirmStatus.REJECTED,
                operation_id=operation_id,
                comment=f"Webhook 通知失败: {e}",
            )

        # 轮询等待确认结果
        poll_interval = 2
        start_time = time.time()
        result_url = f"{self.webhook_url}/result/{operation_id}"

        while time.time() - start_time < self.timeout:
            time.sleep(poll_interval)
            # 优先检查内部提交的结果
            if operation_id in self._confirmation_results:
                return self._confirmation_results.pop(operation_id)
            try:
                with httpx.Client(timeout=5.0) as client:
                    r = client.get(result_url)
                    if r.status_code == 200:
                        data = r.json()
                        status_str = data.get("status", "").upper()
                        if status_str in ("APPROVED", "REJECTED"):
                            return ConfirmationResult(
                                status=ConfirmStatus(status_str),
                                operation_id=operation_id,
                                operator=data.get("operator"),
                                comment=data.get("comment"),
                            )
            except Exception as e:
                logger.debug(f"[HumanConfirm] 轮询结果失败（将重试）: {e}")

        return ConfirmationResult(
            status=ConfirmStatus.TIMEOUT,
            operation_id=operation_id,
            comment=f"等待超时（{self.timeout}秒）",
        )

    def submit_confirmation(
        self,
        operation_id: str,
        approved: bool,
        operator: str = "api_user",
        comment: Optional[str] = None,
    ) -> None:
        """
        供 HTTP API 接口调用，将人工确认结果写入实例

        Args:
            operation_id: 操作 ID（来自 webhook 通知）
            approved: True=批准，False=拒绝
            operator: 操作员名称
            comment: 备注
        """
        self._confirmation_results[operation_id] = ConfirmationResult(
            status=ConfirmStatus.APPROVED if approved else ConfirmStatus.REJECTED,
            operation_id=operation_id,
            operator=operator,
            comment=comment,
        )
