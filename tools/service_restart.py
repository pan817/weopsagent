"""
服务重启工具 - 重启远程服务器上的服务进程（危险操作，需人工确认）

使用 LangChain 1.2.x @tool 装饰器实现，不再继承 BaseTool 类。
此工具在 middleware/human_confirm.py 的 DANGEROUS_TOOLS 集合中，
调用前会自动触发人工确认流程。
"""
import json
import logging
import time
from typing import Optional

import paramiko
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from config.settings import settings
from .base import format_tool_result

logger = logging.getLogger(__name__)


class ServiceRestartInput(BaseModel):
    """服务重启工具输入参数"""
    host: str = Field(description="目标服务器 IP 或主机名")
    service_name: str = Field(description="要重启的服务名称，如 'order-service'")
    restart_command: str = Field(
        default=None,
        description="自定义重启命令，如 'systemctl restart order-service'。不提供时根据服务名自动生成。"
    )
    pre_check_command: str = Field(default=None, description="重启前执行的检查命令（可选）")
    post_check_command: str = Field(default=None, description="重启后执行的验证命令（可选）")


@tool(
    "restart_service",
    args_schema=ServiceRestartInput,
    description="⚠️ 危险操作：通过 SSH 重启远程服务器上的指定服务。支持 systemctl/supervisorctl 自动检测，执行前需人工确认。",
)
def restart_service(
    host: str,
    service_name: str,
    restart_command: str = None,
    pre_check_command: str = None,
    post_check_command: str = None,
) -> str:
    """⚠️ 危险操作：重启远程服务器上的指定服务。此操作会中断服务，执行前需要人工确认。"""
    if host in settings.restart_blacklist:
        result = format_tool_result(
            "restart_service",
            False,
            error=f"主机 {host} 在重启黑名单中，禁止重启",
        )
        return json.dumps(result, ensure_ascii=False)

    start_time = time.time()

    try:
        result = _execute_restart(
            host=host,
            service_name=service_name,
            restart_command=restart_command,
            pre_check_command=pre_check_command,
            post_check_command=post_check_command,
        )
        elapsed = time.time() - start_time
        return json.dumps(
            format_tool_result("restart_service", True, result, elapsed=elapsed),
            ensure_ascii=False,
        )
    except Exception as e:
        elapsed = time.time() - start_time
        logger.error(f"[ServiceRestart] 重启失败 host={host} service={service_name}: {e}")
        return json.dumps(
            format_tool_result("restart_service", False, error=str(e), elapsed=elapsed),
            ensure_ascii=False,
        )


def _execute_restart(
    host: str,
    service_name: str,
    restart_command: Optional[str],
    pre_check_command: Optional[str],
    post_check_command: Optional[str],
) -> dict:
    """通过 SSH 执行重启操作"""
    import os
    key_path = os.path.expanduser(settings.ssh_default_key_path)

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    connect_kwargs: dict = {
        "hostname": host,
        "port": settings.ssh_default_port,
        "username": settings.ssh_default_user,
        "timeout": 15,
    }
    if os.path.exists(key_path):
        connect_kwargs["key_filename"] = key_path

    steps = []

    try:
        ssh.connect(**connect_kwargs)
        logger.info(f"[ServiceRestart] SSH 连接成功 {host}")

        if pre_check_command:
            _, stdout, stderr = ssh.exec_command(pre_check_command)
            steps.append({
                "step": "pre_check",
                "command": pre_check_command,
                "output": stdout.read().decode().strip()[:500],
                "error": stderr.read().decode().strip()[:200] or None,
            })

        if not restart_command:
            restart_command = _auto_detect_restart_command(ssh, service_name)

        logger.info(f"[ServiceRestart] 执行重启命令: {restart_command}")
        _, stdout, stderr = ssh.exec_command(restart_command)
        exit_status = stdout.channel.recv_exit_status()
        restart_output = stdout.read().decode().strip()
        restart_err = stderr.read().decode().strip()

        steps.append({
            "step": "restart",
            "command": restart_command,
            "exit_status": exit_status,
            "output": restart_output[:500],
            "error": restart_err[:200] or None,
            "success": exit_status == 0,
        })

        if exit_status != 0:
            raise RuntimeError(f"重启命令返回非零退出码 {exit_status}: {restart_err}")

        time.sleep(3)

        if post_check_command:
            _, stdout, _ = ssh.exec_command(post_check_command)
            steps.append({
                "step": "post_check",
                "command": post_check_command,
                "output": stdout.read().decode().strip()[:500],
            })
        else:
            _, stdout, _ = ssh.exec_command(f"pgrep -fa '{service_name}' | head -3")
            post_output = stdout.read().decode().strip()
            steps.append({
                "step": "post_check_default",
                "output": post_output[:300],
                "service_running": bool(post_output),
            })

        return {
            "host": host,
            "service_name": service_name,
            "restart_command": restart_command,
            "steps": steps,
            "restart_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    finally:
        ssh.close()


def _auto_detect_restart_command(ssh: paramiko.SSHClient, service_name: str) -> str:
    """自动检测合适的重启命令（systemctl / supervisorctl）"""
    _, stdout, _ = ssh.exec_command(
        f"systemctl is-active {service_name} 2>/dev/null && echo 'systemctl'"
    )
    if "systemctl" in stdout.read().decode():
        return f"systemctl restart {service_name}"

    _, stdout, _ = ssh.exec_command(
        f"supervisorctl status {service_name} 2>/dev/null && echo 'supervisorctl'"
    )
    if "supervisorctl" in stdout.read().decode():
        return f"supervisorctl restart {service_name}"

    return f"systemctl restart {service_name}"
