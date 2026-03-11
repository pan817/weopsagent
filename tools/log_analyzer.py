"""
日志分析工具 - 读取远程服务器日志并统计报错信息

使用 LangChain 1.2.x @tool 装饰器实现，不再继承 BaseTool 类。
"""
import json
import logging
import re
import time
from collections import Counter
from typing import Dict, List, Optional

import paramiko
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from config.settings import settings
from .base import format_tool_result, with_retry

logger = logging.getLogger(__name__)

# 常见报错关键词模式
ERROR_PATTERNS = [
    r"ERROR",
    r"FATAL",
    r"Exception",
    r"Error:",
    r"WARN(?:ING)?",
    r"OutOfMemoryError",
    r"NullPointerException",
    r"StackOverflow",
    r"Connection refused",
    r"Timeout",
    r"timeout",
]


class LogAnalyzerInput(BaseModel):
    """日志分析工具输入参数"""
    host: str = Field(description="目标服务器 IP 或主机名")
    log_path: str = Field(description="日志文件路径，支持通配符，如 /var/log/app/*.log")
    lines: int = Field(default=1000, description="分析最后 N 行日志，默认 1000 行")
    error_keywords: List[str] = Field(
        default=None,
        description="自定义报错关键词列表，不提供时使用默认关键词"
    )
    time_window_minutes: int = Field(
        default=30,
        description="分析最近 N 分钟内的日志，默认 30 分钟"
    )


@tool(
    "analyze_logs",
    args_schema=LogAnalyzerInput,
    description="读取并分析远程服务器上的日志文件，统计 ERROR/WARN/Exception 等报错信息，识别最频繁出现的异常类型和错误样本。",
)
def analyze_logs(
    host: str,
    log_path: str,
    lines: int = 1000,
    error_keywords: List[str] = None,
    time_window_minutes: int = 30,
) -> str:
    """读取并分析远程服务器上的日志文件，统计各类报错信息，识别最频繁出现的异常类型。"""
    start_time = time.time()
    keywords = error_keywords or ERROR_PATTERNS

    try:
        result = _analyze_remote_logs(
            host=host,
            log_path=log_path,
            lines=lines,
            error_keywords=keywords,
            time_window_minutes=time_window_minutes,
        )
        elapsed = time.time() - start_time
        return json.dumps(
            format_tool_result("analyze_logs", True, result, elapsed=elapsed),
            ensure_ascii=False,
        )
    except Exception as e:
        elapsed = time.time() - start_time
        logger.error(f"[LogAnalyzer] 日志分析失败 host={host} path={log_path}: {e}")
        return json.dumps(
            format_tool_result("analyze_logs", False, error=str(e), elapsed=elapsed),
            ensure_ascii=False,
        )


@with_retry(exceptions=(paramiko.SSHException, OSError))
def _analyze_remote_logs(
    host: str,
    log_path: str,
    lines: int,
    error_keywords: List[str],
    time_window_minutes: int,
) -> Dict:
    """通过 SSH 读取并分析日志（带重试）"""
    import os
    key_path = os.path.expanduser(settings.ssh_default_key_path)

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    connect_kwargs: dict = {
        "hostname": host,
        "port": settings.ssh_default_port,
        "username": settings.ssh_default_user,
        "timeout": 10,
    }
    if os.path.exists(key_path):
        connect_kwargs["key_filename"] = key_path

    try:
        ssh.connect(**connect_kwargs)

        cmd = f"tail -n {lines} {log_path} 2>/dev/null || echo 'FILE_NOT_FOUND'"
        _, stdout, stderr = ssh.exec_command(cmd)
        log_content = stdout.read().decode("utf-8", errors="replace")
        err_output = stderr.read().decode("utf-8", errors="replace")

        if "FILE_NOT_FOUND" in log_content or not log_content.strip():
            return {
                "host": host,
                "log_path": log_path,
                "status": "file_not_found",
                "message": f"日志文件不存在或为空: {log_path}",
                "error": err_output,
            }

        log_lines = log_content.split("\n")

        error_counts: Counter = Counter()
        error_samples: Dict[str, List[str]] = {}
        exception_lines: List[str] = []

        for line in log_lines:
            if not line.strip():
                continue
            for pattern in error_keywords:
                if re.search(pattern, line, re.IGNORECASE):
                    match = re.search(pattern, line, re.IGNORECASE)
                    key = match.group(0).upper() if match else pattern
                    error_counts[key] += 1
                    if key not in error_samples:
                        error_samples[key] = []
                    if len(error_samples[key]) < 3:
                        error_samples[key].append(line.strip()[:300])
                    if "Exception" in line or "Error" in line:
                        exception_lines.append(line.strip()[:300])
                    break

        _, stdout, _ = ssh.exec_command(f"stat -c '%y' {log_path} 2>/dev/null")
        last_modified = stdout.read().decode().strip()

        _, stdout, _ = ssh.exec_command(f"du -sh {log_path} 2>/dev/null")
        file_size_out = stdout.read().decode().strip().split()
        file_size = file_size_out[0] if file_size_out else "unknown"

        return {
            "host": host,
            "log_path": log_path,
            "total_lines_analyzed": len(log_lines),
            "last_modified": last_modified,
            "file_size": file_size,
            "error_summary": dict(error_counts.most_common(20)),
            "error_samples": {k: v for k, v in list(error_samples.items())[:10]},
            "recent_exceptions": exception_lines[-20:],
            "has_errors": bool(error_counts),
            "top_error": error_counts.most_common(1)[0] if error_counts else None,
        }
    finally:
        ssh.close()
