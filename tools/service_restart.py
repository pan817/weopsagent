"""
服务重启工具 - 重启远程服务器上的服务进程（危险操作，需人工确认）

注意：此工具在 middleware/human_confirm.py 中被标记为危险工具，
调用前会自动触发人工确认流程。
"""
import json
import logging
import time
from typing import Any, Optional, Type

import paramiko
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from config.settings import settings
from .base import format_tool_result, with_retry

logger = logging.getLogger(__name__)


class ServiceRestartInput(BaseModel):
    """服务重启工具输入参数"""
    host: str = Field(description="目标服务器 IP 或主机名")
    service_name: str = Field(description="要重启的服务名称，如 'order-service'")
    restart_command: str = Field(
        default=None,
        description="自定义重启命令，如 'systemctl restart order-service'。"
                    "不提供时根据服务名自动生成。"
    )
    pre_check_command: str = Field(
        default=None,
        description="重启前执行的检查命令（可选）"
    )
    post_check_command: str = Field(
        default=None,
        description="重启后执行的验证命令（可选）"
    )


class ServiceRestartTool(BaseTool):
    """服务重启工具（危险操作）

    通过 SSH 连接远程服务器并重启指定服务。
    此操作被标记为危险操作，调用前会自动触发人工确认流程。

    支持的重启方式：
    1. systemctl restart <service>
    2. supervisorctl restart <service>
    3. 自定义重启命令
    """
    name: str = "restart_service"
    description: str = (
        "⚠️ 危险操作：重启远程服务器上的指定服务。"
        "此操作会中断服务，执行前需要人工确认。"
        "输入参数: host（服务器地址）, service_name（服务名）, "
        "restart_command（自定义重启命令，可选）"
    )
    args_schema: Type[BaseModel] = ServiceRestartInput

    def _run(
        self,
        host: str,
        service_name: str,
        restart_command: str = None,
        pre_check_command: str = None,
        post_check_command: str = None,
    ) -> str:
        """执行服务重启"""
        # 检查主机是否在黑名单
        if host in settings.restart_blacklist:
            result = format_tool_result(
                "restart_service",
                False,
                error=f"主机 {host} 在重启黑名单中，禁止重启",
            )
            return json.dumps(result, ensure_ascii=False)

        start_time = time.time()

        try:
            result = self._execute_restart(
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
        self,
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

            # 执行前置检查
            if pre_check_command:
                _, stdout, stderr = ssh.exec_command(pre_check_command)
                pre_check_output = stdout.read().decode().strip()
                pre_check_err = stderr.read().decode().strip()
                steps.append({
                    "step": "pre_check",
                    "command": pre_check_command,
                    "output": pre_check_output[:500],
                    "error": pre_check_err[:200] if pre_check_err else None,
                })

            # 构建重启命令
            if not restart_command:
                # 优先尝试 systemctl，然后 supervisorctl
                restart_command = self._auto_detect_restart_command(ssh, service_name)

            # 执行重启
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
                "error": restart_err[:200] if restart_err else None,
                "success": exit_status == 0,
            })

            if exit_status != 0:
                raise RuntimeError(f"重启命令返回非零退出码 {exit_status}: {restart_err}")

            # 等待服务启动
            time.sleep(3)

            # 执行后置验证
            if post_check_command:
                _, stdout, stderr = ssh.exec_command(post_check_command)
                post_output = stdout.read().decode().strip()
                steps.append({
                    "step": "post_check",
                    "command": post_check_command,
                    "output": post_output[:500],
                })
            else:
                # 默认验证进程是否存活
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

    def _auto_detect_restart_command(self, ssh: paramiko.SSHClient, service_name: str) -> str:
        """自动检测合适的重启命令"""
        # 尝试 systemctl
        _, stdout, _ = ssh.exec_command(
            f"systemctl is-active {service_name} 2>/dev/null && echo 'systemctl'"
        )
        if "systemctl" in stdout.read().decode():
            return f"systemctl restart {service_name}"

        # 尝试 supervisorctl
        _, stdout, _ = ssh.exec_command(
            f"supervisorctl status {service_name} 2>/dev/null && echo 'supervisorctl'"
        )
        if "supervisorctl" in stdout.read().decode():
            return f"supervisorctl restart {service_name}"

        # 默认 systemctl
        return f"systemctl restart {service_name}"

    async def _arun(self, *args, **kwargs) -> str:
        return self._run(*args, **kwargs)
