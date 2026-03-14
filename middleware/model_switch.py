"""
动态模型切换中间件 - 根据上下文在 LLM 调用前自动切换模型

基于 LangChain 1.2.x 的 AgentMiddleware 机制实现，
通过重写 before_model hook，在每次 LLM 调用前根据规则动态替换模型。

典型场景：
- 监控采集类任务 → 使用低成本模型（如 gpt-4o-mini）
- 根因分析类任务 → 使用高能力模型（如 gpt-4o）
- 重试/回环时降级 → 避免反复消耗高价模型配额
- 按 Agent 名称切换 → 不同子 Agent 使用不同模型

使用方式：
    middleware = ModelSwitchMiddleware(
        rules=[
            ModelRule(agent_name="monitor_agent", model="gpt-4o-mini"),
            ModelRule(agent_name="analysis_agent", model="gpt-4o"),
            ModelRule(keyword="简单", model="gpt-4o-mini"),
        ],
        default_model="gpt-4o",
    )
    agent = create_agent(..., middleware=[middleware])
"""
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from langchain.agents.middleware.types import AgentMiddleware
from langchain_openai import ChatOpenAI

from config.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class ModelRule:
    """
    模型切换规则

    规则按优先级匹配（列表中靠前的优先），首条匹配即生效。

    Attributes:
        model: 匹配后切换到的目标模型名称
        agent_name: 匹配 Agent 名称（从 runtime.config.tags 或 configurable 中读取）
        keyword: 匹配消息内容中的关键词
        min_call_index: 最小 LLM 调用序号（用于重试降级：第 N 次调用后切换）
        condition: 自定义匹配函数 (state, runtime) -> bool
    """
    model: str
    agent_name: Optional[str] = None
    keyword: Optional[str] = None
    min_call_index: Optional[int] = None
    condition: Optional[Callable[[Any, Any], bool]] = None


class ModelSwitchMiddleware(AgentMiddleware):
    """
    动态模型切换 AgentMiddleware（LangChain 1.2.x）

    通过 before_model hook 在每次 LLM 调用前检查规则，
    按需将 runtime.model 替换为目标模型实例。

    规则匹配顺序：按 rules 列表顺序，首条命中即生效。
    无规则命中时保持 default_model（若设置）或不做任何切换。
    """

    def __init__(
        self,
        rules: Optional[List[ModelRule]] = None,
        default_model: Optional[str] = None,
    ):
        """
        Args:
            rules: 模型切换规则列表，按顺序匹配
            default_model: 默认模型名称，无规则命中时使用（None 表示不切换）
        """
        self.rules = rules or []
        self.default_model = default_model
        self._call_index: int = 0
        self._lock = threading.Lock()
        # 缓存已创建的 ChatOpenAI 实例，避免重复创建
        self._model_cache: dict = {}
        self._current_model_name: Optional[str] = None

    def before_agent(self, state: Any, runtime: Any) -> Any:
        """Agent 循环开始时重置调用计数器"""
        with self._lock:
            self._call_index = 0
            self._current_model_name = None
        return None

    def before_model(self, state: Any, runtime: Any) -> Any:
        """
        LLM 调用前 - 根据规则动态切换模型

        检查所有规则，首条匹配的规则决定使用哪个模型。
        通过修改 runtime.model 实现运行时模型切换。
        """
        with self._lock:
            self._call_index += 1
            call_index = self._call_index

            # 逐条匹配规则
            target_model = self._match_rule(state, runtime)

            if not target_model and self.default_model:
                target_model = self.default_model

            if not target_model:
                return None

            # 与当前模型相同则跳过
            if target_model == self._current_model_name:
                return None

            # 切换模型
            model_instance = self._get_or_create_model(target_model)
            runtime.model = model_instance
            self._current_model_name = target_model

        logger.info(
            f"[ModelSwitch] 切换模型 → {target_model} "
            f"(call_index={call_index})"
        )
        return None

    def _match_rule(self, state: Any, runtime: Any) -> Optional[str]:
        """按顺序匹配规则，返回首条命中规则的目标模型名称"""
        for rule in self.rules:
            if self._rule_matches(rule, state, runtime):
                logger.debug(
                    f"[ModelSwitch] 规则命中: model={rule.model} "
                    f"agent_name={rule.agent_name} keyword={rule.keyword} "
                    f"min_call_index={rule.min_call_index}"
                )
                return rule.model
        return None

    def _rule_matches(self, rule: ModelRule, state: Any, runtime: Any) -> bool:
        """检查单条规则是否匹配当前上下文"""

        # 匹配 Agent 名称
        if rule.agent_name is not None:
            agent_name = self._extract_agent_name(runtime)
            if agent_name != rule.agent_name:
                return False

        # 匹配关键词（在最新消息中搜索）
        if rule.keyword is not None:
            last_content = self._extract_last_content(state)
            if rule.keyword not in last_content:
                return False

        # 匹配最小调用序号（用于重试降级）
        if rule.min_call_index is not None:
            if self._call_index < rule.min_call_index:
                return False

        # 自定义条件函数
        if rule.condition is not None:
            try:
                if not rule.condition(state, runtime):
                    return False
            except Exception as e:
                logger.warning(f"[ModelSwitch] 自定义条件函数异常: {e}")
                return False

        return True

    @staticmethod
    def _extract_agent_name(runtime: Any) -> str:
        """从 runtime 中提取当前 Agent 名称"""
        try:
            config = getattr(runtime, "config", None)
            # 从 tags 中提取 agent 名称
            tags = getattr(config, "tags", []) or []
            for tag in tags:
                if isinstance(tag, str) and tag.startswith("agent:"):
                    return tag.split(":", 1)[1]
            # 从 configurable 中提取
            configurable = getattr(config, "configurable", None) or {}
            return configurable.get("agent_name", "")
        except Exception:
            return ""

    @staticmethod
    def _extract_last_content(state: Any) -> str:
        """从 state 中提取最新一条消息的文本内容"""
        try:
            messages = getattr(state, "messages", []) or []
            if not messages:
                return ""
            last_msg = messages[-1]
            content = getattr(last_msg, "content", "")
            if isinstance(content, str):
                return content
            elif isinstance(content, list):
                texts = [
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                return "\n".join(texts)
            return str(content)
        except Exception:
            return ""

    def _get_or_create_model(self, model_name: str) -> ChatOpenAI:
        """获取或创建指定模型的 ChatOpenAI 实例（带缓存）"""
        if model_name not in self._model_cache:
            self._model_cache[model_name] = ChatOpenAI(
                model=model_name,
                api_key=settings.openai_api_key,
                base_url=settings.openai_api_base,
                temperature=settings.openai_temperature,
                max_tokens=settings.openai_max_tokens,
                streaming=False,
            )
            logger.info(f"[ModelSwitch] 创建模型实例: {model_name}")
        return self._model_cache[model_name]
