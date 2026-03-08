"""
工具模块 - 提供所有监控和故障处理工具，以及 ToolRegistry 统一管理类
"""
from typing import Any, Dict, List, Optional, Set

from .process_monitor import monitor_process
from .redis_monitor import monitor_redis
from .mq_monitor import monitor_mq
from .db_monitor import monitor_database
from .log_analyzer import analyze_logs
from .service_restart import restart_service
from .notification import send_notification
from .knowledge_store import store_knowledge


class ToolRegistry:
    """
    工具注册管理类

    统一管理所有 LangChain @tool 函数，支持按名称和分组检索。

    分组说明：
    - monitor:      只读监控采集类工具（monitor_process / monitor_redis / monitor_mq /
                    monitor_database / analyze_logs），适合 MonitorAgent 使用
    - recovery:     故障恢复操作类工具（restart_service / store_knowledge），
                    适合 RecoveryAgent 使用，其中 restart_service 为危险操作
    - notification: 通知告警类工具（send_notification），适合 NotificationAgent 使用
    - all:          全部工具

    使用示例：
        registry = get_tool_registry()
        monitor_tools  = registry.get_group("monitor")
        recovery_tools = registry.get_group("recovery")
        tool_obj       = registry.get("monitor_process")
    """

    def __init__(self) -> None:
        # {tool_name: tool_object}
        self._tools: Dict[str, Any] = {}
        # {group_name: set of tool_names}
        self._groups: Dict[str, Set[str]] = {}

    def register(self, tool_obj: Any, groups: Optional[List[str]] = None) -> "ToolRegistry":
        """
        注册一个工具

        :param tool_obj: @tool 装饰的函数对象（具有 .name 属性）
        :param groups:   工具所属分组列表，不传则仅加入 "all" 分组
        :return: self，支持链式调用
        """
        name: str = tool_obj.name
        self._tools[name] = tool_obj

        all_groups = {"all"} | set(groups or [])
        for g in all_groups:
            self._groups.setdefault(g, set()).add(name)

        return self

    def get(self, name: str) -> Any:
        """按名称获取工具，不存在时抛出 KeyError"""
        if name not in self._tools:
            raise KeyError(f"工具 '{name}' 未注册。已注册工具: {self.list_names()}")
        return self._tools[name]

    def get_group(self, group: str) -> List[Any]:
        """
        获取指定分组下的所有工具列表

        :param group: 分组名称（monitor / recovery / notification / all）
        :return: 工具对象列表
        :raises KeyError: 分组不存在时
        """
        if group not in self._groups:
            raise KeyError(f"分组 '{group}' 未定义。已有分组: {self.list_groups()}")
        return [self._tools[n] for n in sorted(self._groups[group])]

    def get_all(self) -> List[Any]:
        """获取全部已注册工具列表"""
        return list(self._tools.values())

    def list_names(self) -> List[str]:
        """列出全部已注册工具名称"""
        return sorted(self._tools.keys())

    def list_groups(self) -> List[str]:
        """列出全部已定义分组名称"""
        return sorted(self._groups.keys())

    def describe(self) -> str:
        """
        返回所有工具的简要描述文本，便于调试和日志输出。

        格式示例：
            [monitor]  monitor_process   - 监控指定服务器上的服务进程状态...
            [recovery] restart_service   - ⚠️ 危险操作：重启远程服务器...
        """
        lines = []
        for group in self.list_groups():
            if group == "all":
                continue
            for name in sorted(self._groups[group]):
                tool_obj = self._tools[name]
                desc = (tool_obj.description or "")[:60].replace("\n", " ")
                lines.append(f"  [{group:<12}] {name:<25} - {desc}")
        return "\n".join(lines)


# ===== 全局单例 =====
_registry: Optional[ToolRegistry] = None


def get_tool_registry() -> ToolRegistry:
    """
    获取全局 ToolRegistry 单例（进程级懒加载）

    首次调用时自动注册所有内置工具。
    """
    global _registry
    if _registry is None:
        _registry = ToolRegistry()

        # 监控采集工具（只读，无危险操作）
        monitor_group = "monitor"
        _registry.register(monitor_process, groups=[monitor_group])
        _registry.register(monitor_redis,   groups=[monitor_group])
        _registry.register(monitor_mq,      groups=[monitor_group])
        _registry.register(monitor_database, groups=[monitor_group])
        _registry.register(analyze_logs,    groups=[monitor_group])

        # 故障恢复工具（含危险操作）
        recovery_group = "recovery"
        _registry.register(restart_service,  groups=[recovery_group])
        _registry.register(store_knowledge,  groups=[recovery_group])

        # 通知工具
        notification_group = "notification"
        _registry.register(send_notification, groups=[notification_group])

    return _registry


def get_all_tools() -> List[Any]:
    """兼容旧代码：获取所有工具列表"""
    return get_tool_registry().get_all()


__all__ = [
    # 工具函数（@tool 装饰器）
    "monitor_process",
    "monitor_redis",
    "monitor_mq",
    "monitor_database",
    "analyze_logs",
    "restart_service",
    "send_notification",
    "store_knowledge",
    # 注册管理
    "ToolRegistry",
    "get_tool_registry",
    "get_all_tools",
]
