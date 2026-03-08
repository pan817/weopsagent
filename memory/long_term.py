"""
长期记忆模块 - 基于向量数据库（ChromaDB）和 RAG 机制实现知识库检索

知识库分三个层级：
1. general：通用故障处理方案（service-agnostic）
2. scenario：具体业务场景处理方案
3. history：历史故障处理案例

每个 Markdown 文件一条记录，支持语义检索。
"""
import hashlib
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import chromadb
from chromadb.config import Settings as ChromaSettings
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings

from config.settings import settings

logger = logging.getLogger(__name__)


class LongTermMemory:
    """
    长期记忆管理器 - 基于 ChromaDB + OpenAI Embeddings 的 RAG 知识库

    支持：
    - 从 data/ 目录批量加载 Markdown 知识文件
    - 语义相似度检索（RAG）
    - 动态添加新的处理经验
    - 按类别（general/scenario/history）分库存储
    """

    def __init__(self):
        """初始化长期记忆，连接 ChromaDB"""
        self._embeddings = self._get_embeddings()
        self._stores: Dict[str, Chroma] = {}
        self._chroma_client = chromadb.PersistentClient(
            path=settings.chroma_persist_dir,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        # 初始化三个知识库集合
        self._init_stores()

    def _get_embeddings(self):
        """获取 Embedding 模型"""
        model = settings.embedding_model
        # 如果是 OpenAI 模型
        if "ada" in model or "embedding" in model.lower():
            return OpenAIEmbeddings(
                model=model,
                api_key=settings.openai_api_key,
                base_url=settings.openai_api_base,
            )
        # 本地 sentence-transformers 模型
        try:
            from langchain_community.embeddings import HuggingFaceEmbeddings
            return HuggingFaceEmbeddings(
                model_name=model,
                model_kwargs={"device": "cpu"},
            )
        except ImportError:
            logger.warning("[LongTermMemory] HuggingFace embeddings 不可用，回退到 OpenAI")
            return OpenAIEmbeddings(
                model="text-embedding-ada-002",
                api_key=settings.openai_api_key,
                base_url=settings.openai_api_base,
            )

    def _init_stores(self):
        """初始化各知识库的 Chroma 向量存储"""
        collections = {
            "general": settings.chroma_collection_general,
            "scenario": settings.chroma_collection_scenarios,
            "history": settings.chroma_collection_history,
        }
        for category, collection_name in collections.items():
            self._stores[category] = Chroma(
                client=self._chroma_client,
                collection_name=collection_name,
                embedding_function=self._embeddings,
            )
        logger.info("[LongTermMemory] 知识库向量存储初始化完成")

    def load_knowledge_base(self, force_reload: bool = False) -> Dict[str, int]:
        """
        从 data/ 目录加载所有 Markdown 知识文件到向量数据库

        Args:
            force_reload: 是否强制重新加载（会清空现有数据）

        Returns:
            每个类别加载的文档数量
        """
        data_dir = settings.data_dir
        counts = {}

        category_dirs = {
            "general": data_dir / "general",
            "scenario": data_dir / "scenarios",
            "history": data_dir / "history",
        }

        for category, dir_path in category_dirs.items():
            if not dir_path.exists():
                logger.warning(f"[LongTermMemory] 知识库目录不存在: {dir_path}")
                counts[category] = 0
                continue

            md_files = list(dir_path.glob("*.md"))
            if not md_files:
                counts[category] = 0
                continue

            documents = []
            for md_file in md_files:
                doc = self._load_markdown_file(md_file, category)
                if doc:
                    documents.append(doc)

            if documents:
                if force_reload:
                    self._stores[category].delete_collection()
                    self._init_stores()

                # 使用文件路径的 hash 作为 ID，避免重复加载
                ids = [
                    hashlib.md5(doc.metadata["source"].encode()).hexdigest()
                    for doc in documents
                ]
                try:
                    self._stores[category].add_documents(documents, ids=ids)
                    counts[category] = len(documents)
                    logger.info(
                        f"[LongTermMemory] 已加载 {len(documents)} 条知识 "
                        f"到 {category} 集合"
                    )
                except Exception as e:
                    logger.error(f"[LongTermMemory] 加载 {category} 知识库失败: {e}")
                    counts[category] = 0
            else:
                counts[category] = 0

        return counts

    def _load_markdown_file(self, file_path: Path, category: str) -> Optional[Document]:
        """加载单个 Markdown 文件为 Document"""
        try:
            content = file_path.read_text(encoding="utf-8")
            # 提取文件名作为标题（去掉 .md 后缀）
            title = file_path.stem.replace("_", " ").replace("-", " ").title()

            return Document(
                page_content=content,
                metadata={
                    "source": str(file_path),
                    "filename": file_path.name,
                    "title": title,
                    "category": category,
                    "loaded_at": time.time(),
                },
            )
        except Exception as e:
            logger.error(f"[LongTermMemory] 加载文件失败 {file_path}: {e}")
            return None

    def search(
        self,
        query: str,
        category: Optional[str] = None,
        top_k: int = 5,
        score_threshold: float = 0.3,
    ) -> List[Document]:
        """
        基于语义相似度检索相关知识

        Args:
            query: 检索查询文本（如故障描述）
            category: 限定检索的知识库类别（general/scenario/history），None 表示全部
            top_k: 返回最相关的 K 条结果
            score_threshold: 相似度阈值（0-1），低于此值的结果会被过滤

        Returns:
            按相关性排序的 Document 列表
        """
        if category and category in self._stores:
            # 检索指定类别
            categories_to_search = [category]
        else:
            # 检索所有类别
            categories_to_search = list(self._stores.keys())

        all_results: List[tuple] = []

        for cat in categories_to_search:
            store = self._stores.get(cat)
            if store is None:
                continue
            try:
                # 使用带分数的相似度检索
                results = store.similarity_search_with_relevance_scores(
                    query,
                    k=top_k,
                )
                for doc, score in results:
                    if score >= score_threshold:
                        doc.metadata["relevance_score"] = round(score, 4)
                        all_results.append((doc, score))
            except Exception as e:
                logger.warning(f"[LongTermMemory] 检索 {cat} 失败: {e}")

        # 按相关性排序，取 top_k
        all_results.sort(key=lambda x: x[1], reverse=True)
        return [doc for doc, _ in all_results[:top_k]]

    def search_all_categories(
        self,
        query: str,
        top_k_each: int = 3,
        score_threshold: Optional[float] = None,
    ) -> Dict[str, List[Document]]:
        """
        分别检索三个知识库类别，返回各类别的结果

        Args:
            query: 检索查询
            top_k_each: 每个类别返回的最大条数
            score_threshold: 相关性阈值，None 则使用 settings.rag_score_threshold

        Returns:
            {"general": [...], "scenario": [...], "history": [...]}
        """
        threshold = score_threshold if score_threshold is not None else settings.rag_score_threshold
        results = {}
        for category in ["general", "scenario", "history"]:
            docs = self.search(query, category=category, top_k=top_k_each,
                               score_threshold=threshold)
            results[category] = docs
        return results

    def format_context(
        self,
        query: str,
        top_k: int = 5,
        max_chars: int = 3000,
        score_threshold: Optional[float] = None,
    ) -> str:
        """
        检索并格式化知识库内容，用于注入 AnalysisAgent prompt。

        相关性过滤逻辑：
        - 每条检索结果必须达到 score_threshold（默认 settings.rag_score_threshold=0.65）
        - 低于阈值的结果直接丢弃，不传入 prompt，避免低质量内容误导分析
        - 所有结果均被过滤时，返回明确的"无历史参考"说明，引导 Agent 依赖监控数据

        Args:
            query: 故障描述（直接作为语义检索 query）
            top_k: 检索结果总数上限
            max_chars: 注入 prompt 的最大字符数
            score_threshold: 相关性阈值，None 则读取 settings.rag_score_threshold

        Returns:
            格式化的知识库内容字符串
        """
        threshold = score_threshold if score_threshold is not None else settings.rag_score_threshold
        results = self.search_all_categories(
            query,
            top_k_each=top_k // 3 + 1,
            score_threshold=threshold,
        )

        total_found = sum(len(docs) for docs in results.values())
        logger.info(
            f"[LongTermMemory] RAG 检索完成 threshold={threshold:.2f} "
            f"命中: general={len(results['general'])} "
            f"scenario={len(results['scenario'])} "
            f"history={len(results['history'])}"
        )

        if total_found == 0:
            logger.info(
                f"[LongTermMemory] 未找到相关性 >= {threshold:.2f} 的知识，"
                "返回无历史参考提示"
            )
            return (
                "【知识库检索结果】\n"
                f"未找到与当前故障相关性 ≥ {threshold:.2f} 的历史案例或处理方案。\n"
                "请完全依赖本次采集的监控数据进行独立分析，不要假设存在历史经验。"
            )

        sections = []

        # 通用处理方案
        if results["general"]:
            sections.append("### 通用故障处理方案")
            for doc in results["general"]:
                score = doc.metadata.get("relevance_score", 0)
                sections.append(
                    f"**{doc.metadata.get('title', '未知')}** (相关性: {score:.2f})\n"
                    f"{doc.page_content[:500]}"
                )

        # 场景处理方案
        if results["scenario"]:
            sections.append("\n### 具体场景处理方案")
            for doc in results["scenario"]:
                score = doc.metadata.get("relevance_score", 0)
                sections.append(
                    f"**{doc.metadata.get('title', '未知')}** (相关性: {score:.2f})\n"
                    f"{doc.page_content[:500]}"
                )

        # 历史故障案例
        if results["history"]:
            sections.append("\n### 历史故障处理案例")
            for doc in results["history"]:
                score = doc.metadata.get("relevance_score", 0)
                sections.append(
                    f"**{doc.metadata.get('title', '未知')}** (相关性: {score:.2f})\n"
                    f"{doc.page_content[:500]}"
                )

        context = "\n\n".join(sections)
        # 截断以控制 Prompt 长度
        return context[:max_chars] if len(context) > max_chars else context

    def store_fault_experience(
        self,
        title: str,
        fault_description: str,
        root_cause: str,
        solution: str,
        category: str = "history",
        tags: str = "",
        effectiveness: str = "confirmed",
    ) -> str:
        """
        将故障处理经验存入知识库

        Args:
            title: 措施标题
            fault_description: 故障现象
            root_cause: 根因分析
            solution: 解决方案
            category: 存储类别
            tags: 标签
            effectiveness: 有效性评估

        Returns:
            文档 ID
        """
        # 构建标准化的知识文档内容
        content = f"""# {title}

## 故障现象
{fault_description}

## 根因分析
{root_cause}

## 解决方案
{solution}

## 标签
{tags}

## 有效性
{effectiveness}

## 记录时间
{time.strftime('%Y-%m-%d %H:%M:%S')}
"""
        doc_id = hashlib.md5(
            f"{title}{time.time()}".encode()
        ).hexdigest()

        doc = Document(
            page_content=content,
            metadata={
                "source": f"dynamic:{doc_id}",
                "title": title,
                "category": category,
                "tags": tags,
                "effectiveness": effectiveness,
                "created_at": time.time(),
            },
        )

        store = self._stores.get(category)
        if not store:
            raise ValueError(f"未知的知识库类别: {category}")

        store.add_documents([doc], ids=[doc_id])
        logger.info(f"[LongTermMemory] 已存储新知识: {title} -> {category}")

        # 同时写入 Markdown 文件（持久化）
        self._save_to_markdown(title, content, category, doc_id)

        return doc_id

    def _save_to_markdown(
        self,
        title: str,
        content: str,
        category: str,
        doc_id: str,
    ) -> None:
        """将知识文档保存为 Markdown 文件"""
        category_dir_map = {
            "general": settings.data_dir / "general",
            "scenario": settings.data_dir / "scenarios",
            "history": settings.data_dir / "history",
        }
        target_dir = category_dir_map.get(category, settings.data_dir / "history")
        target_dir.mkdir(parents=True, exist_ok=True)

        # 用标题生成文件名
        safe_name = "".join(c for c in title if c.isalnum() or c in (" ", "-", "_")).strip()
        safe_name = safe_name.replace(" ", "_")[:50]
        filename = f"{safe_name}_{doc_id[:8]}.md"

        file_path = target_dir / filename
        file_path.write_text(content, encoding="utf-8")
        logger.info(f"[LongTermMemory] 已保存知识文件: {file_path}")


# 全局单例
_global_long_term_memory: Optional[LongTermMemory] = None


def get_long_term_memory() -> LongTermMemory:
    """获取全局长期记忆单例"""
    global _global_long_term_memory
    if _global_long_term_memory is None:
        _global_long_term_memory = LongTermMemory()
    return _global_long_term_memory
