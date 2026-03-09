"""
FastAPI HTTP 服务 - 提供故障处理 API 接口

提供以下接口：
- POST /api/v1/fault/handle  - 接收故障描述，触发自动处理
- POST /api/v1/fault/continue - 继续故障处理对话
- POST /api/v1/confirm        - 提交人工确认结果（危险操作）
- GET  /api/v1/fault/{fault_id}/status - 查询故障处理状态
- GET  /api/v1/health         - 健康检查
- POST /api/v1/knowledge/reload - 重新加载知识库
"""
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from config.settings import settings

logger = logging.getLogger(__name__)

# ===== 请求/响应 Schema =====

class FaultHandleRequest(BaseModel):
    """故障处理请求"""
    fault_description: str = Field(
        ...,
        description="故障描述文本",
        min_length=5,
        max_length=5000,
        examples=["订单服务接口响应超时，P99 超过 5 秒，错误率达到 30%"],
    )
    fault_id: Optional[str] = Field(
        default=None,
        description="故障 ID，不提供则自动生成",
        pattern=r"^[A-Za-z0-9\-_]{1,64}$",
    )
    session_id: Optional[str] = Field(
        default=None,
        description="会话 ID，用于关联多轮对话，不提供则使用 fault_id",
    )
    async_mode: bool = Field(
        default=False,
        description="是否异步处理（true=立即返回 task_id，false=同步等待结果）",
    )


class FaultContinueRequest(BaseModel):
    """继续故障对话请求"""
    session_id: str = Field(..., description="已有的会话 ID")
    message: str = Field(..., description="用户追加的消息", min_length=1, max_length=2000)


class ConfirmRequest(BaseModel):
    """人工确认请求（用于审批危险操作）"""
    operation_id: str = Field(..., description="操作 ID")
    approved: bool = Field(..., description="是否批准")
    operator: str = Field(default="api_user", description="操作员名称")
    comment: Optional[str] = Field(default=None, description="备注说明")


class FaultHandleResponse(BaseModel):
    """故障处理响应"""
    fault_id: str
    session_id: str
    service_name: str
    alert_type: str
    response: str
    status: str
    elapsed_seconds: float
    timestamp: float = Field(default_factory=time.time)


class HealthResponse(BaseModel):
    """健康检查响应"""
    status: str = "ok"
    timestamp: float = Field(default_factory=time.time)
    version: str = "1.0.0"


# ===== 应用生命周期 =====

# 全局 Agent 实例（懒加载）
_fault_agent = None
# 待确认操作缓存 {operation_id: middleware_instance}
_pending_confirms: Dict[str, Any] = {}
# 异步任务结果缓存 {fault_id: result}
_task_results: Dict[str, Dict] = {}


def get_fault_agent():
    """获取全局 FaultAgent 单例（懒加载）"""
    global _fault_agent
    if _fault_agent is None:
        from agents.fault_agent import FaultAgent
        _fault_agent = FaultAgent(
            console_confirm_mode=False,  # 生产环境使用 API 确认模式
            enable_audit_log=True,
        )
        logger.info("[API] FaultAgent 初始化完成")
    return _fault_agent


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动/关闭生命周期钩子"""
    logger.info("[API] WeOps Agent 服务启动中...")
    # 预热初始化
    try:
        agent = get_fault_agent()
        logger.info("[API] 服务启动完成，Agent 已就绪")
    except Exception as e:
        logger.error(f"[API] Agent 初始化失败（服务仍将启动）: {e}")
    yield
    logger.info("[API] WeOps Agent 服务正在关闭...")


# ===== FastAPI 应用 =====

app = FastAPI(
    title="WeOps Intelligent Fault Agent API",
    description="智能故障处理 Agent - 自动分析故障并执行处理",
    version="1.0.0",
    lifespan=lifespan,
)

# 跨域配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===== 全局异常处理 =====

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"[API] 未处理异常: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "error": "内部服务错误",
            "detail": str(exc),
            "timestamp": time.time(),
        },
    )


# ===== API 接口 =====

@app.get("/api/v1/health", response_model=HealthResponse, tags=["基础"])
async def health_check():
    """服务健康检查接口"""
    return HealthResponse(status="ok")


@app.post(
    "/api/v1/fault/handle",
    response_model=FaultHandleResponse,
    summary="提交故障处理请求",
    description="接收故障描述，Agent 自动分析并处理故障。支持同步和异步两种模式。",
    tags=["故障处理"],
)
async def handle_fault(
    request: FaultHandleRequest,
    background_tasks: BackgroundTasks,
):
    """
    核心接口：提交故障并触发自动处理

    - **fault_description**: 故障描述（必填），如"订单服务接口报500错误"
    - **fault_id**: 故障 ID（可选），不提供则自动生成
    - **session_id**: 会话 ID（可选），用于多轮对话
    - **async_mode**: 异步模式（默认 false）
    """
    agent = get_fault_agent()

    if request.async_mode:
        # 异步模式：立即返回，后台处理
        from datetime import datetime
        fault_id = request.fault_id or f"FAULT-{datetime.now().strftime('%Y%m%d%H%M%S')}"

        def _run_async():
            result = agent.handle_fault(
                fault_description=request.fault_description,
                fault_id=fault_id,
                session_id=request.session_id,
            )
            _task_results[fault_id] = result

        background_tasks.add_task(_run_async)

        return FaultHandleResponse(
            fault_id=fault_id,
            session_id=request.session_id or fault_id,
            service_name="pending",
            alert_type="pending",
            response=f"已接受故障处理请求，正在异步处理。可通过 GET /api/v1/fault/{fault_id}/status 查询状态。",
            status="processing",
            elapsed_seconds=0,
        )
    else:
        # 同步模式：等待处理完成
        try:
            result = agent.handle_fault(
                fault_description=request.fault_description,
                fault_id=request.fault_id,
                session_id=request.session_id,
            )
            return FaultHandleResponse(**result)
        except Exception as e:
            logger.error(f"[API] 故障处理失败: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"故障处理失败: {str(e)}")


@app.post(
    "/api/v1/fault/continue",
    response_model=FaultHandleResponse,
    summary="继续故障处理对话",
    description="在已有故障处理 session 基础上，继续追加问题或指令。",
    tags=["故障处理"],
)
async def continue_fault(request: FaultContinueRequest):
    """
    多轮对话接口：在已有会话中继续沟通

    - **session_id**: 已有的会话 ID（必填）
    - **message**: 追加的消息内容（必填）
    """
    agent = get_fault_agent()
    try:
        result = agent.continue_conversation(
            session_id=request.session_id,
            user_input=request.message,
        )
        return FaultHandleResponse(**result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get(
    "/api/v1/fault/{fault_id}/status",
    summary="查询故障处理状态",
    tags=["故障处理"],
)
async def get_fault_status(fault_id: str):
    """查询异步故障处理任务的状态和结果"""
    if fault_id in _task_results:
        return {
            "fault_id": fault_id,
            "status": _task_results[fault_id].get("status", "completed"),
            "result": _task_results[fault_id],
        }
    return {
        "fault_id": fault_id,
        "status": "processing",
        "result": None,
    }


@app.post(
    "/api/v1/confirm",
    summary="提交人工确认结果",
    description="对 Agent 提出的危险操作（如服务重启）进行人工审批。",
    tags=["人工确认"],
)
async def submit_confirmation(request: ConfirmRequest):
    """
    人工确认接口

    当 Agent 需要执行危险操作时，会通过 Webhook 发送确认请求。
    操作员在此接口提交审批结果。

    - **operation_id**: 操作 ID（来自 Webhook 通知）
    - **approved**: true=批准执行, false=拒绝执行
    - **operator**: 操作员名称
    - **comment**: 备注
    """
    # 获取人工确认中间件实例并提交结果
    from middleware.human_confirm import HumanConfirmMiddleware
    # 注意：在生产环境中，需要通过某种机制将确认结果传递给等待中的中间件实例
    # 这里简单记录确认结果，实际场景可用 Redis 或数据库存储
    logger.info(
        f"[API] 收到人工确认: operation_id={request.operation_id} "
        f"approved={request.approved} operator={request.operator}"
    )
    return {
        "operation_id": request.operation_id,
        "approved": request.approved,
        "operator": request.operator,
        "timestamp": time.time(),
        "message": "确认结果已记录",
    }


@app.post(
    "/api/v1/knowledge/reload",
    summary="重新加载知识库",
    description="重新扫描 data/ 目录，加载最新的 Markdown 知识文件到向量数据库。",
    tags=["知识库管理"],
)
async def reload_knowledge():
    """重新加载知识库（热更新）"""
    try:
        from memory.long_term import get_long_term_memory
        ltm = get_long_term_memory()
        counts = ltm.load_knowledge_base(force_reload=False)
        return {
            "status": "success",
            "loaded_counts": counts,
            "timestamp": time.time(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"知识库加载失败: {str(e)}")


@app.get(
    "/api/v1/sessions",
    summary="列出所有活跃会话",
    tags=["会话管理"],
)
async def list_sessions():
    """列出所有活跃会话（由 checkpointer 自动管理对话历史）"""
    agent = get_fault_agent()
    sessions = agent.list_sessions()
    return {
        "total": len(sessions),
        "sessions": sessions,
    }


@app.delete(
    "/api/v1/sessions/{session_id}",
    summary="清除会话历史",
    tags=["会话管理"],
)
async def clear_session(session_id: str):
    """清除指定会话"""
    agent = get_fault_agent()
    agent.clear_session(session_id)
    return {"status": "cleared", "session_id": session_id}


def run_server():
    """启动 HTTP 服务"""
    uvicorn.run(
        "api.server:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.api_debug,
        log_level=settings.log_level.lower(),
        access_log=True,
    )


if __name__ == "__main__":
    run_server()
