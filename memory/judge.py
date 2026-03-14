"""
LLM 质量评估器 - 对故障处理经验进行质量评分，决定是否写入 ES/Redis

评估维度：
1. 方案有效性（根因 + 解决方案是否可复用）
2. 描述清晰度（是否有明确的技术细节）
3. 方案可操作性（步骤是否具体，非泛话）

评分 >= memory_judge_score_threshold 时写入 ES/Redis，否则仅保留 ChromaDB。
"""
import json
import logging
from dataclasses import dataclass, field
from typing import List, Optional

from config.settings import settings

logger = logging.getLogger(__name__)

_JUDGE_PROMPT = """你是一个故障知识库的质量评估专家。请评估以下故障处理经验的质量，判断是否值得存入高优先级知识库。

## 待评估的故障经验

**标题**: {title}

**故障现象**:
{fault_description}

**根因分析**:
{root_cause}

**解决方案**:
{solution}

## 评估维度（各 33 分，合计 100 分）

1. **可复用性**（0-33）: 根因分析是否清晰、有明确的技术原因，未来类似故障可参考
2. **清晰度**（0-33）: 故障描述和根因是否具体，包含服务名、错误信息、关键指标等技术细节
3. **可操作性**（0-34）: 解决方案是否有具体步骤，而非"重启服务"这类泛化表述

## 输出格式（严格 JSON，不要添加额外文字）

```json
{{
  "score": 0.85,
  "reason": "根因明确（连接池耗尽），方案有具体参数调整步骤，可复用性强",
  "tags": ["mysql", "connection-pool", "timeout"],
  "summary": "MySQL 连接池耗尽导致服务超时，调整 maxPoolSize 和 connectionTimeout 后恢复"
}}
```

其中 score 为 0-1 之间的小数（百分制总分 / 100）。"""


@dataclass
class JudgeResult:
    """LLM 质量评估结果"""
    score: float
    should_store: bool
    reason: str = ""
    tags: List[str] = field(default_factory=list)
    summary: str = ""


class LLMJudge:
    """
    LLM 质量评估器

    使用低成本 LLM（默认与系统 openai_model 相同）对故障处理经验进行评分，
    评分 >= threshold 才允许写入 ES/Redis 热路径，避免低质量数据污染快速检索层。

    设计为同步调用，由 MemoryManager 在后台线程中异步触发。
    """

    def __init__(self):
        self._model_name = settings.memory_judge_model or settings.openai_model
        self._threshold = settings.memory_judge_score_threshold
        self._llm = None

    def _get_llm(self):
        if self._llm is None:
            from langchain_openai import ChatOpenAI
            self._llm = ChatOpenAI(
                model=self._model_name,
                api_key=settings.openai_api_key,
                base_url=settings.openai_api_base,
                temperature=0,
                max_tokens=512,
            )
        return self._llm

    def evaluate(
        self,
        title: str,
        fault_description: str,
        root_cause: str,
        solution: str,
    ) -> JudgeResult:
        """
        评估故障处理经验质量

        Returns:
            JudgeResult，包含分数、是否存入热路径、标签、摘要
        """
        if not settings.memory_judge_enabled:
            # 门控关闭：全部放行（score=1.0）
            return JudgeResult(score=1.0, should_store=True, reason="judge disabled")

        try:
            prompt = _JUDGE_PROMPT.format(
                title=title,
                fault_description=fault_description[:600],
                root_cause=root_cause[:600],
                solution=solution[:600],
            )
            response = self._get_llm().invoke(prompt)
            content = response.content if hasattr(response, "content") else str(response)

            # 提取 JSON（兼容 LLM 输出带 ``` 代码块的情况）
            raw = content.strip()
            if "```" in raw:
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            data = json.loads(raw.strip())

            score = float(data.get("score", 0))
            result = JudgeResult(
                score=score,
                should_store=score >= self._threshold,
                reason=data.get("reason", ""),
                tags=data.get("tags", []),
                summary=data.get("summary", ""),
            )
            logger.info(
                f"[LLMJudge] 评估完成 title={title!r} "
                f"score={score:.2f} should_store={result.should_store}"
            )
            return result

        except Exception as e:
            logger.warning(f"[LLMJudge] 评估失败（放行写入）: {e}")
            # 评估失败时保守策略：放行，避免丢失数据
            return JudgeResult(score=0.5, should_store=True, reason=f"judge error: {e}")
