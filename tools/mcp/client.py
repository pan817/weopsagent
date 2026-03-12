"""
MCP HTTP 客户端基类 - 为各 MCP Server 提供统一的 HTTP 调用封装

MCP Server 通常通过 HTTP/SSE 提供工具调用接口，
本模块封装通用的请求/响应处理逻辑，各 MCP 工具继承使用。
"""
import json
import logging
import time
from typing import Any, Dict, Optional

import httpx

from tools.base import format_tool_result

logger = logging.getLogger(__name__)

# 默认超时配置
DEFAULT_TIMEOUT = 30.0


def mcp_request(
    url: str,
    method: str = "GET",
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = DEFAULT_TIMEOUT,
    auth: Optional[tuple] = None,
) -> Dict[str, Any]:
    """
    发送 HTTP 请求到 MCP Server

    Args:
        url: 完整请求 URL
        method: HTTP 方法（GET/POST/PUT/DELETE）
        params: URL 查询参数
        json_body: JSON 请求体
        headers: 自定义请求头
        timeout: 超时秒数
        auth: Basic Auth (username, password) 元组

    Returns:
        解析后的 JSON 响应，失败时返回 {"error": ...}
    """
    start_time = time.time()
    _headers = {"Content-Type": "application/json"}
    if headers:
        _headers.update(headers)

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.request(
                method=method.upper(),
                url=url,
                params=params,
                json=json_body,
                headers=_headers,
                auth=auth,
            )
            elapsed = time.time() - start_time

            if response.status_code >= 400:
                error_text = response.text[:500]
                logger.warning(
                    f"[MCP] {method} {url} → {response.status_code}: {error_text}"
                )
                return {
                    "error": f"HTTP {response.status_code}: {error_text}",
                    "status_code": response.status_code,
                    "elapsed": round(elapsed, 2),
                }

            # 尝试解析 JSON
            try:
                data = response.json()
            except (json.JSONDecodeError, ValueError):
                data = {"raw_text": response.text[:2000]}

            logger.debug(f"[MCP] {method} {url} → 200 ({elapsed:.2f}s)")
            return data

    except httpx.TimeoutException:
        elapsed = time.time() - start_time
        logger.error(f"[MCP] {method} {url} 超时 ({elapsed:.2f}s)")
        return {"error": f"请求超时 ({timeout}s)", "elapsed": round(elapsed, 2)}
    except Exception as e:
        elapsed = time.time() - start_time
        logger.error(f"[MCP] {method} {url} 异常: {e}")
        return {"error": f"请求失败: {type(e).__name__}: {e}", "elapsed": round(elapsed, 2)}


def format_mcp_result(
    tool_name: str,
    data: Any,
    elapsed: float = 0.0,
) -> str:
    """将 MCP 响应格式化为工具结果 JSON 字符串"""
    if isinstance(data, dict) and "error" in data:
        return json.dumps(
            format_tool_result(tool_name, False, error=data["error"], elapsed=elapsed),
            ensure_ascii=False,
        )
    return json.dumps(
        format_tool_result(tool_name, True, data=data, elapsed=elapsed),
        ensure_ascii=False,
    )
