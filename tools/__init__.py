"""
工具模块 - 提供所有监控和故障处理工具
"""
from .process_monitor import ProcessMonitorTool
from .redis_monitor import RedisMonitorTool
from .mq_monitor import MQMonitorTool
from .db_monitor import DBMonitorTool
from .log_analyzer import LogAnalyzerTool
from .service_restart import ServiceRestartTool
from .notification import NotificationTool
from .knowledge_store import StoreKnowledgeTool

def get_all_tools():
    """获取所有工具实例列表"""
    return [
        ProcessMonitorTool(),
        RedisMonitorTool(),
        MQMonitorTool(),
        DBMonitorTool(),
        LogAnalyzerTool(),
        ServiceRestartTool(),
        NotificationTool(),
        StoreKnowledgeTool(),
    ]

__all__ = [
    "ProcessMonitorTool",
    "RedisMonitorTool",
    "MQMonitorTool",
    "DBMonitorTool",
    "LogAnalyzerTool",
    "ServiceRestartTool",
    "NotificationTool",
    "StoreKnowledgeTool",
    "get_all_tools",
]
