"""
DingTalk MCP 工具 - 通过钉钉 Webhook 发送通知消息

提供文本消息和 Markdown 消息两个工具。
支持 @指定人员、自定义标题等功能。

典型用法：
- 故障告警通知（Markdown 格式，含服务名、错误详情）
- 恢复通知
- 升级通知（@负责人）
"""
import hashlib
import hmac
import json
import logging
import time
import base64
import urllib.parse
from typing import List, Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from config.settings import settings
from .client import mcp_request, format_mcp_result

logger = logging.getLogger(__name__)


def _build_webhook_url() -> str:
    """构建钉钉 Webhook URL（含签名，如果配置了 secret）"""
    webhook_url = settings.mcp_dingtalk_webhook
    secret = settings.mcp_dingtalk_secret

    if not secret:
        return webhook_url

    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))

    sep = "&" if "?" in webhook_url else "?"
    return f"{webhook_url}{sep}timestamp={timestamp}&sign={sign}"


class DingTalkTextInput(BaseModel):
    """钉钉文本消息输入"""
    content: str = Field(description="消息内容文本")
    at_mobiles: str = Field(default="", description="@指定手机号（逗号分隔），如 '13800138000,13900139000'")
    at_all: bool = Field(default=False, description="是否 @所有人")


class DingTalkMarkdownInput(BaseModel):
    """钉钉 Markdown 消息输入"""
    title: str = Field(description="消息标题（在通知栏显示）")
    content: str = Field(description="Markdown 格式正文内容，支持标题、列表、链接、加粗等")
    at_mobiles: str = Field(default="", description="@指定手机号（逗号分隔）")
    at_all: bool = Field(default=False, description="是否 @所有人")


@tool("dingtalk_send_text", args_schema=DingTalkTextInput)
def dingtalk_send_text(
    content: str,
    at_mobiles: str = "",
    at_all: bool = False,
) -> str:
    """通过钉钉 Webhook 发送纯文本通知消息。可 @指定人员或 @所有人。
    适用于简单告警通知和状态更新。"""
    start_time = time.time()

    mobiles = [m.strip() for m in at_mobiles.split(",") if m.strip()] if at_mobiles else []

    body = {
        "msgtype": "text",
        "text": {"content": content},
        "at": {
            "atMobiles": mobiles,
            "isAtAll": at_all,
        },
    }

    data = mcp_request(
        url=_build_webhook_url(),
        method="POST",
        json_body=body,
        timeout=10.0,
    )

    elapsed = time.time() - start_time

    success = isinstance(data, dict) and data.get("errcode", -1) == 0
    if success:
        return format_mcp_result("dingtalk_send_text", {
            "status": "sent",
            "content_preview": content[:100],
            "at_mobiles": mobiles,
            "at_all": at_all,
        }, elapsed)

    return format_mcp_result("dingtalk_send_text", data, elapsed)


@tool("dingtalk_send_markdown", args_schema=DingTalkMarkdownInput)
def dingtalk_send_markdown(
    title: str,
    content: str,
    at_mobiles: str = "",
    at_all: bool = False,
) -> str:
    """通过钉钉 Webhook 发送 Markdown 格式通知消息。支持富文本格式（标题、列表、加粗、链接等）。
    适用于故障告警报告、根因分析结果通知、恢复确认通知。"""
    start_time = time.time()

    mobiles = [m.strip() for m in at_mobiles.split(",") if m.strip()] if at_mobiles else []

    body = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": content,
        },
        "at": {
            "atMobiles": mobiles,
            "isAtAll": at_all,
        },
    }

    data = mcp_request(
        url=_build_webhook_url(),
        method="POST",
        json_body=body,
        timeout=10.0,
    )

    elapsed = time.time() - start_time

    success = isinstance(data, dict) and data.get("errcode", -1) == 0
    if success:
        return format_mcp_result("dingtalk_send_markdown", {
            "status": "sent",
            "title": title,
            "content_preview": content[:200],
            "at_mobiles": mobiles,
            "at_all": at_all,
        }, elapsed)

    return format_mcp_result("dingtalk_send_markdown", data, elapsed)
