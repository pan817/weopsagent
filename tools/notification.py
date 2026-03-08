"""
通知工具 - 支持钉钉、Slack、邮件等多渠道通知

使用 LangChain 1.2.x @tool 装饰器实现，不再继承 BaseTool 类。
"""
import json
import logging
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Optional

import httpx
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from config.settings import settings
from .base import format_tool_result

logger = logging.getLogger(__name__)

# 严重程度对应的图标
SEVERITY_ICONS = {
    "critical": "🔴",
    "warning": "🟡",
    "info": "🔵",
    "recovery": "🟢",
}


class NotificationInput(BaseModel):
    """通知工具输入参数"""
    message: str = Field(description="要发送的通知消息内容（支持 Markdown）")
    title: str = Field(default="WeOps 故障通知", description="通知标题")
    severity: str = Field(
        default="warning",
        description="严重程度: critical（严重）, warning（警告）, info（信息）, recovery（恢复）"
    )
    channels: List[str] = Field(
        default=None,
        description="通知渠道列表，可选: dingtalk, slack, email。不填则使用所有已配置渠道。"
    )
    recipients: List[str] = Field(
        default=None,
        description="邮件收件人列表（覆盖配置文件中的默认收件人）"
    )


@tool("send_notification", args_schema=NotificationInput)
def send_notification(
    message: str,
    title: str = "WeOps 故障通知",
    severity: str = "warning",
    channels: List[str] = None,
    recipients: List[str] = None,
) -> str:
    """向运维人员发送通知消息，支持钉钉、Slack、邮件等多渠道。可用于故障告警、处理进展通知、故障恢复通知等场景。"""
    start_time = time.time()
    icon = SEVERITY_ICONS.get(severity, "⚪")
    full_title = f"{icon} {title}"

    if channels:
        active_channels = channels
    else:
        active_channels = []
        if settings.notify_dingtalk_webhook:
            active_channels.append("dingtalk")
        if settings.notify_slack_webhook:
            active_channels.append("slack")
        if settings.notify_email_smtp_host:
            active_channels.append("email")

    if not active_channels:
        logger.warning("[Notification] 未配置任何通知渠道，消息未发送")
        return json.dumps(
            format_tool_result(
                "send_notification", False,
                error="未配置任何通知渠道",
                data={"message": message, "channels_tried": []}
            ),
            ensure_ascii=False,
        )

    results = {}
    for channel in active_channels:
        try:
            if channel == "dingtalk":
                results["dingtalk"] = _send_dingtalk(full_title, message, severity)
            elif channel == "slack":
                results["slack"] = _send_slack(full_title, message, severity)
            elif channel == "email":
                email_list = recipients or settings.email_recipients_list
                results["email"] = _send_email(full_title, message, email_list)
        except Exception as e:
            logger.error(f"[Notification] {channel} 发送失败: {e}")
            results[channel] = {"success": False, "error": str(e)}

    elapsed = time.time() - start_time
    overall_success = any(r.get("success", False) for r in results.values())

    return json.dumps(
        format_tool_result(
            "send_notification",
            overall_success,
            data={"channels": results, "message_preview": message[:200]},
            elapsed=elapsed,
        ),
        ensure_ascii=False,
    )


def _send_dingtalk(title: str, message: str, severity: str) -> dict:
    """发送钉钉群消息"""
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": f"## {title}\n\n{message}\n\n> 发送时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        },
    }
    with httpx.Client(timeout=10.0) as client:
        resp = client.post(settings.notify_dingtalk_webhook, json=payload)
        resp.raise_for_status()
        data = resp.json()
        success = data.get("errcode", -1) == 0
        return {"success": success, "response": data}


def _send_slack(title: str, message: str, severity: str) -> dict:
    """发送 Slack 消息"""
    color_map = {
        "critical": "#FF0000",
        "warning": "#FFA500",
        "info": "#0066CC",
        "recovery": "#00CC00",
    }
    payload = {
        "attachments": [{
            "title": title,
            "text": message,
            "color": color_map.get(severity, "#808080"),
            "footer": "WeOps Agent",
            "ts": int(time.time()),
        }]
    }
    with httpx.Client(timeout=10.0) as client:
        resp = client.post(settings.notify_slack_webhook, json=payload)
        resp.raise_for_status()
        return {"success": True, "status_code": resp.status_code}


def _send_email(title: str, message: str, recipients: Optional[List[str]]) -> dict:
    """发送邮件通知"""
    if not recipients:
        return {"success": False, "error": "没有收件人"}

    msg = MIMEMultipart("alternative")
    msg["Subject"] = title
    msg["From"] = settings.notify_email_user
    msg["To"] = ", ".join(recipients)

    html_content = f"""
    <html><body>
    <h2>{title}</h2>
    <pre style="background-color:#f4f4f4;padding:15px;border-radius:5px;">
    {message}
    </pre>
    <p style="color:#888;font-size:12px;">发送时间: {time.strftime('%Y-%m-%d %H:%M:%S')}</p>
    </body></html>
    """
    msg.attach(MIMEText(message, "plain", "utf-8"))
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    with smtplib.SMTP_SSL(
        settings.notify_email_smtp_host,
        settings.notify_email_smtp_port
    ) as server:
        server.login(settings.notify_email_user, settings.notify_email_password)
        server.sendmail(
            settings.notify_email_user,
            recipients,
            msg.as_string(),
        )

    return {"success": True, "recipients": recipients}
