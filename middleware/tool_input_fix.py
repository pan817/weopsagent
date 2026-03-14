"""
工具参数修正中间件 - 自动修复 LLM 生成的工具调用参数格式错误

基于 LangChain 1.2.x 的 AgentMiddleware 机制实现，
通过 wrap_tool_call hook 在工具执行前拦截参数，自动修正常见格式问题，
修正失败时返回清晰的错误提示让 LLM 重试，而不是直接抛异常中断。

常见的 LLM 参数错误：
1. 类型不匹配：把 int 传成 "6"、把 bool 传成 "true"
2. 多余字段：传入 schema 中不存在的参数
3. JSON 字符串：把 dict/list 参数传成 JSON 字符串
4. 缺少必填字段：漏传 required 参数
5. null/None：传 null 给 non-optional 字段
6. 列表传成逗号分隔字符串："a,b,c" → ["a","b","c"]

使用方式：
    middleware = ToolInputFixMiddleware(
        auto_coerce=True,    # 自动类型转换（默认开启）
        strip_extra=True,    # 自动移除多余字段（默认开启）
        max_retries=3,       # 连续参数错误超过 N 次后跳过修正，防止死循环
    )
    agent = create_agent(..., middleware=[middleware])
"""
import json
import logging
import threading
from typing import Any, Callable, Dict, List, Optional, Set

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

logger = logging.getLogger(__name__)


class ToolInputFixMiddleware(AgentMiddleware):
    """
    工具参数修正 AgentMiddleware（LangChain 1.2.x）

    通过 wrap_tool_call hook 在工具执行前拦截参数：
    1. 尝试自动修正格式错误（类型转换、移除多余字段、解析 JSON 字符串等）
    2. 修正后再调用 handler 执行工具
    3. 若修正失败，返回 ToolMessage 错误提示，LLM 可据此重试
    """

    def __init__(
        self,
        auto_coerce: bool = True,
        strip_extra: bool = True,
        max_retries: int = 3,
    ):
        """
        Args:
            auto_coerce: 是否自动做类型转换（str→int、str→bool 等）
            strip_extra: 是否自动移除 schema 中不存在的多余字段
            max_retries: 同一工具连续参数错误超过此次数后停止修正，防止 LLM 死循环
        """
        self.auto_coerce = auto_coerce
        self.strip_extra = strip_extra
        self.max_retries = max_retries
        self._lock = threading.Lock()
        # {tool_name: consecutive_error_count}
        self._error_counts: Dict[str, int] = {}
        # 统计
        self._total_fixed: int = 0
        self._total_rejected: int = 0

    def before_agent(self, state: Any, runtime: Any) -> Any:
        """Agent 循环开始时重置计数器"""
        with self._lock:
            self._error_counts.clear()
            self._total_fixed = 0
            self._total_rejected = 0
        return None

    def after_agent(self, state: Any, runtime: Any) -> Any:
        """Agent 循环结束时输出统计"""
        with self._lock:
            fixed, rejected = self._total_fixed, self._total_rejected
        if fixed > 0 or rejected > 0:
            logger.info(
                f"[ToolInputFix] 统计: "
                f"自动修正={fixed} 次, "
                f"返回错误提示={rejected} 次"
            )
        return None

    def wrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        """
        工具调用前拦截并修正参数

        流程：
        1. 提取 tool_call.args 和对应工具的 args_schema
        2. 尝试自动修正参数（类型转换、移除多余字段）
        3. 用 args_schema.model_validate() 验证
        4. 验证通过 → 用修正后的参数调用 handler
        5. 验证失败 → 返回 ToolMessage 错误提示
        """
        tool_call = getattr(request, "tool_call", {}) or {}
        tool_name = tool_call.get("name", "unknown")
        tool_args = tool_call.get("args", {})
        tool_id = tool_call.get("id", "")

        # 获取工具的 args_schema（Pydantic Model）
        tool_obj = getattr(request, "tool", None)
        args_schema = getattr(tool_obj, "args_schema", None) if tool_obj else None

        if args_schema is None:
            # 没有 schema，直接放行
            return handler(request)

        # 检查连续错误次数，防止死循环
        with self._lock:
            error_count = self._error_counts.get(tool_name, 0)
        if error_count >= self.max_retries:
            logger.warning(
                f"[ToolInputFix] {tool_name} 连续参数错误超过 {self.max_retries} 次，跳过修正"
            )
            return handler(request)

        # 尝试修正参数
        fixed_args, fixes_applied = self._fix_args(tool_args, args_schema)

        if fixes_applied:
            logger.info(
                f"[ToolInputFix] {tool_name} 参数已自动修正: {fixes_applied}"
            )
            with self._lock:
                self._total_fixed += 1
            # 更新 request 中的参数
            tool_call["args"] = fixed_args

        # 用 Pydantic 验证修正后的参数
        try:
            args_schema.model_validate(fixed_args)
        except Exception as e:
            # 验证失败，返回错误提示让 LLM 重试
            with self._lock:
                self._error_counts[tool_name] = self._error_counts.get(tool_name, 0) + 1
                self._total_rejected += 1
                err_count = self._error_counts[tool_name]

            error_msg = self._build_error_message(tool_name, fixed_args, args_schema, e)
            logger.warning(
                f"[ToolInputFix] {tool_name} 参数验证失败 "
                f"(第 {err_count} 次): {e}"
            )

            return ToolMessage(
                content=error_msg,
                tool_call_id=tool_id,
            )

        # 验证通过，重置错误计数
        with self._lock:
            self._error_counts[tool_name] = 0

        # 执行工具（仍然 try/catch，处理运行时参数错误）
        try:
            return handler(request)
        except (TypeError, ValueError) as e:
            # 运行时参数错误（如函数签名不匹配），返回提示
            with self._lock:
                self._error_counts[tool_name] = self._error_counts.get(tool_name, 0) + 1
                self._total_rejected += 1

            error_msg = (
                f"工具 {tool_name} 执行时参数错误: {type(e).__name__}: {e}\n"
                f"传入参数: {json.dumps(fixed_args, ensure_ascii=False, default=str)[:500]}\n"
                f"请检查参数类型和格式后重试。"
            )
            logger.warning(f"[ToolInputFix] {tool_name} 运行时参数错误: {e}")

            return ToolMessage(
                content=error_msg,
                tool_call_id=tool_id,
            )

    def _fix_args(
        self,
        args: Dict[str, Any],
        schema: Any,
    ) -> tuple:
        """
        尝试自动修正参数

        Returns:
            (fixed_args, fixes_applied) - 修正后的参数和修正记录列表
        """
        if not isinstance(args, dict):
            # args 本身不是 dict（如 LLM 传了字符串），尝试解析
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                    if isinstance(args, dict):
                        return self._fix_args(args, schema)
                except (json.JSONDecodeError, ValueError):
                    pass
            return args if isinstance(args, dict) else {}, []

        fixed = dict(args)
        fixes = []

        # 获取 schema 的字段信息
        schema_fields = {}
        try:
            for field_name, field_info in schema.model_fields.items():
                schema_fields[field_name] = field_info
        except AttributeError:
            return fixed, fixes

        # 1. 移除多余字段
        if self.strip_extra:
            extra_keys = set(fixed.keys()) - set(schema_fields.keys())
            for key in extra_keys:
                del fixed[key]
                fixes.append(f"移除多余字段 '{key}'")

        # 2. 自动类型转换
        if self.auto_coerce:
            for field_name, field_info in schema_fields.items():
                if field_name not in fixed:
                    continue

                value = fixed[field_name]
                if value is None:
                    continue

                target_type = self._get_field_type(field_info)
                if target_type is None:
                    continue

                coerced = self._coerce_value(value, target_type)
                if coerced is not value and coerced is not _SKIP:
                    fixed[field_name] = coerced
                    fixes.append(
                        f"'{field_name}': {type(value).__name__}({repr(value)[:50]}) → {type(coerced).__name__}"
                    )

        # 3. 处理 null → 移除（让 Pydantic 使用默认值）
        null_keys = [k for k, v in fixed.items() if v is None and k in schema_fields]
        for key in null_keys:
            field_info = schema_fields[key]
            if field_info.is_required():
                continue  # 必填字段保留 None，让验证报错
            del fixed[key]
            fixes.append(f"移除 null 字段 '{key}'（将使用默认值）")

        return fixed, fixes

    @staticmethod
    def _get_field_type(field_info: Any) -> Optional[type]:
        """从 Pydantic FieldInfo 提取目标类型"""
        try:
            annotation = field_info.annotation
            # 处理 Optional[X] → X
            origin = getattr(annotation, "__origin__", None)
            if origin is type(None):
                return None
            # Union[X, None] → X
            args = getattr(annotation, "__args__", None)
            if args:
                non_none = [a for a in args if a is not type(None)]
                if non_none:
                    return non_none[0]
            if isinstance(annotation, type):
                return annotation
        except Exception:
            pass
        return None

    @staticmethod
    def _coerce_value(value: Any, target_type: type) -> Any:
        """
        尝试将 value 转换为 target_type

        成功返回转换后的值，不需要转换返回原值，失败返回 _SKIP
        """
        if isinstance(value, target_type):
            return value

        try:
            # str → int
            if target_type is int and isinstance(value, str):
                return int(value)

            # str → float
            if target_type is float and isinstance(value, str):
                return float(value)

            # str → bool
            if target_type is bool and isinstance(value, str):
                if value.lower() in ("true", "1", "yes", "on"):
                    return True
                if value.lower() in ("false", "0", "no", "off"):
                    return False

            # int → bool（0/1）
            if target_type is bool and isinstance(value, int):
                return bool(value)

            # float → int（无小数部分时）
            if target_type is int and isinstance(value, float):
                if value == int(value):
                    return int(value)

            # str → list（逗号分隔）
            if target_type is list and isinstance(value, str):
                try:
                    parsed = json.loads(value)
                    if isinstance(parsed, list):
                        return parsed
                except (json.JSONDecodeError, ValueError):
                    pass
                return [item.strip() for item in value.split(",") if item.strip()]

            # str → dict（JSON 字符串）
            if target_type is dict and isinstance(value, str):
                parsed = json.loads(value)
                if isinstance(parsed, dict):
                    return parsed

        except (ValueError, TypeError, json.JSONDecodeError):
            pass

        return _SKIP

    @staticmethod
    def _build_error_message(
        tool_name: str,
        args: Dict[str, Any],
        schema: Any,
        error: Exception,
    ) -> str:
        """构建清晰的参数错误提示，帮助 LLM 修正参数"""
        # 提取 schema 字段描述
        field_docs = []
        try:
            for name, info in schema.model_fields.items():
                required = "必填" if info.is_required() else f"可选, 默认={info.default}"
                desc = info.description or ""
                type_str = str(info.annotation).replace("typing.", "")
                field_docs.append(f"  - {name} ({type_str}, {required}): {desc}")
        except Exception:
            pass

        schema_doc = "\n".join(field_docs) if field_docs else "  （无法获取参数定义）"

        return (
            f"工具 {tool_name} 参数格式错误，请修正后重试。\n\n"
            f"错误详情: {error}\n\n"
            f"你传入的参数:\n"
            f"  {json.dumps(args, ensure_ascii=False, default=str)[:500]}\n\n"
            f"正确的参数定义:\n{schema_doc}\n\n"
            f"请严格按照参数定义传入正确的类型和格式。"
        )


# 内部标记：表示类型转换失败，不做修改
class _SkipType:
    pass

_SKIP = _SkipType()
