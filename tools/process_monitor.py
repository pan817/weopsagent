"""
服务进程监控工具 - 检查服务进程状态、CPU/内存使用率等
"""
import json
import logging
import time
from typing import Any, Optional, Type

import paramiko
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from config.settings import settings
from .base import format_tool_result, safe_execute, with_retry

logger = logging.getLogger(__name__)


class ProcessMonitorInput(BaseModel):
    """进程监控工具输入参数"""
    host: str = Field(description="目标服务器 IP 或主机名")
    service_name: str = Field(description="要检查的服务进程名称，如 'order-service', 'nginx'")
    port: int = Field(default=None, description="SSH 端口，默认使用配置值")
    username: str = Field(default=None, description="SSH 用户名，默认使用配置值")


class ProcessMonitorTool(BaseTool):
    """服务进程监控工具

    通过 SSH 连接远程服务器，检查指定服务进程的运行状态，
    包括进程是否存活、CPU 使用率、内存使用率、进程启动时间等。
    """
    name: str = "monitor_process"
    description: str = (
        "监控指定服务器上的服务进程状态。"
        "可以检查进程是否在运行、CPU 使用率、内存占用情况。"
        "输入参数: host（服务器地址）, service_name（服务进程名）"
    )
    args_schema: Type[BaseModel] = ProcessMonitorInput

    def _run(
        self,
        host: str,
        service_name: str,
        port: int = None,
        username: str = None,
    ) -> str:
        """同步执行进程监控"""
        start_time = time.time()
        ssh_port = port or settings.ssh_default_port
        ssh_user = username or settings.ssh_default_user
        key_path = settings.ssh_default_key_path

        try:
            result = self._check_process_via_ssh(
                host=host,
                service_name=service_name,
                port=ssh_port,
                username=ssh_user,
                key_path=key_path,
            )
            elapsed = time.time() - start_time
            return json.dumps(
                format_tool_result("monitor_process", True, result, elapsed=elapsed),
                ensure_ascii=False,
            )
        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(f"[ProcessMonitor] 监控失败 host={host} service={service_name}: {e}")
            return json.dumps(
                format_tool_result("monitor_process", False, error=str(e), elapsed=elapsed),
                ensure_ascii=False,
            )

    @with_retry(exceptions=(paramiko.SSHException, OSError))
    def _check_process_via_ssh(
        self,
        host: str,
        service_name: str,
        port: int,
        username: str,
        key_path: str,
    ) -> dict:
        """通过 SSH 执行远程进程检查"""
        import os
        key_path = os.path.expanduser(key_path)

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs: dict = {
            "hostname": host,
            "port": port,
            "username": username,
            "timeout": 10,
        }
        if os.path.exists(key_path):
            connect_kwargs["key_filename"] = key_path
        else:
            # 尝试使用 SSH Agent
            connect_kwargs["allow_agent"] = True

        try:
            ssh.connect(**connect_kwargs)

            # 检查进程是否存在
            _, stdout, _ = ssh.exec_command(
                f"pgrep -fa '{service_name}' | head -5"
            )
            processes = stdout.read().decode().strip()
            process_running = bool(processes)

            # 获取 CPU 和内存使用率
            cpu_mem = ""
            if process_running:
                _, stdout, _ = ssh.exec_command(
                    f"ps aux | grep '{service_name}' | grep -v grep | "
                    f"awk '{{sum_cpu+=$3; sum_mem+=$4; count++}} END "
                    f"{{printf \"cpu=%.1f%% mem=%.1f%% count=%d\", sum_cpu, sum_mem, count}}'"
                )
                cpu_mem = stdout.read().decode().strip()

            # 获取系统整体资源
            _, stdout, _ = ssh.exec_command(
                "uptime && free -m | grep Mem | awk '{print \"mem_total=\"$2\"MB mem_used=\"$3\"MB mem_free=\"$4\"MB\"}'"
            )
            system_info = stdout.read().decode().strip()

            return {
                "host": host,
                "service_name": service_name,
                "process_running": process_running,
                "process_list": processes[:500] if processes else "",
                "resource_usage": cpu_mem,
                "system_info": system_info,
            }
        finally:
            ssh.close()

    async def _arun(self, *args, **kwargs) -> str:
        """异步执行（委托给同步方法）"""
        return self._run(*args, **kwargs)
