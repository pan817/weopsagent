"""
记忆模块测试用例

测试短期记忆和长期记忆的核心功能。
"""
import os
import sys
import tempfile
import pytest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestShortTermMemory:
    """短期记忆测试"""

    def test_add_and_get_messages(self):
        """测试添加和获取消息"""
        from memory.short_term import ShortTermMemory

        mem = ShortTermMemory(max_messages=10)
        mem.add_user_message("session-1", "订单服务宕机了")
        mem.add_ai_message("session-1", "我开始分析订单服务的故障...")

        messages = mem.get_messages("session-1")
        assert len(messages) == 2
        assert messages[0].content == "订单服务宕机了"
        assert messages[1].content == "我开始分析订单服务的故障..."

    def test_session_isolation(self):
        """测试不同 session 的隔离"""
        from memory.short_term import ShortTermMemory

        mem = ShortTermMemory()
        mem.add_user_message("session-1", "消息A")
        mem.add_user_message("session-2", "消息B")

        msgs1 = mem.get_messages("session-1")
        msgs2 = mem.get_messages("session-2")

        assert len(msgs1) == 1
        assert msgs1[0].content == "消息A"
        assert len(msgs2) == 1
        assert msgs2[0].content == "消息B"

    def test_message_trimming(self):
        """测试消息超限时的自动裁剪"""
        from memory.short_term import ShortTermMemory

        max_msgs = 4
        mem = ShortTermMemory(max_messages=max_msgs)

        # 添加超过上限的消息
        for i in range(6):
            mem.add_user_message("sess", f"消息{i}")

        messages = mem.get_messages("sess")
        assert len(messages) == max_msgs
        # 保留的是最新的消息
        assert messages[-1].content == "消息5"

    def test_clear_session(self):
        """测试清除 session"""
        from memory.short_term import ShortTermMemory

        mem = ShortTermMemory()
        mem.add_user_message("sess", "test")
        assert len(mem.get_messages("sess")) == 1

        mem.clear_session("sess")
        assert len(mem.get_messages("sess")) == 0

    def test_export_import(self):
        """测试导出和导入历史消息"""
        from memory.short_term import ShortTermMemory

        mem = ShortTermMemory()
        mem.add_user_message("sess", "用户问题")
        mem.add_ai_message("sess", "AI回答")

        exported = mem.export_session("sess")
        assert len(exported) == 2
        assert exported[0]["content"] == "用户问题"

        # 导入到新 session
        mem.import_session("sess-2", exported)
        msgs = mem.get_messages("sess-2")
        assert len(msgs) == 2

    def test_get_messages_as_text(self):
        """测试格式化为文本"""
        from memory.short_term import ShortTermMemory

        mem = ShortTermMemory()
        mem.add_user_message("sess", "测试消息")
        text = mem.get_messages_as_text("sess")
        assert "测试消息" in text
        assert "用户" in text

    def test_empty_session_text(self):
        """测试空 session 的文本格式"""
        from memory.short_term import ShortTermMemory
        mem = ShortTermMemory()
        text = mem.get_messages_as_text("nonexistent-session")
        assert "无历史" in text or text == "（无历史对话）"

    def test_list_sessions(self):
        """测试列出所有活跃会话"""
        from memory.short_term import ShortTermMemory
        mem = ShortTermMemory()
        mem.add_user_message("session-a", "msg")
        mem.add_user_message("session-b", "msg")
        sessions = mem.list_sessions()
        assert "session-a" in sessions
        assert "session-b" in sessions


class TestLongTermMemoryInit:
    """长期记忆初始化测试（不依赖真实 Chroma）"""

    @patch("memory.long_term.chromadb.PersistentClient")
    @patch("memory.long_term.OpenAIEmbeddings")
    def test_init_with_mock(self, mock_embeddings, mock_chroma):
        """测试长期记忆初始化"""
        from memory.long_term import LongTermMemory

        mock_embeddings.return_value = MagicMock()
        mock_chroma.return_value = MagicMock()

        # 模拟 Chroma store
        mock_store = MagicMock()
        mock_chroma.return_value.get_or_create_collection.return_value = MagicMock()

        with patch("memory.long_term.Chroma") as mock_chroma_store:
            mock_chroma_store.return_value = mock_store
            ltm = LongTermMemory()
            assert ltm is not None

    @patch("memory.long_term.chromadb.PersistentClient")
    @patch("memory.long_term.OpenAIEmbeddings")
    @patch("memory.long_term.Chroma")
    def test_search_returns_list(self, mock_chroma, mock_emb, mock_client):
        """测试搜索返回正确类型"""
        from memory.long_term import LongTermMemory
        from langchain_core.documents import Document

        mock_emb.return_value = MagicMock()
        mock_client.return_value = MagicMock()

        mock_store = MagicMock()
        mock_store.similarity_search_with_relevance_scores.return_value = [
            (Document(page_content="Redis 连接超时处理方案", metadata={"category": "general"}), 0.85),
            (Document(page_content="历史案例：Redis OOM", metadata={"category": "history"}), 0.72),
        ]
        mock_chroma.return_value = mock_store

        ltm = LongTermMemory()
        results = ltm.search("Redis 连接失败")
        assert isinstance(results, list)

    @patch("memory.long_term.chromadb.PersistentClient")
    @patch("memory.long_term.OpenAIEmbeddings")
    @patch("memory.long_term.Chroma")
    def test_format_context_empty(self, mock_chroma, mock_emb, mock_client):
        """测试空检索结果的格式化"""
        from memory.long_term import LongTermMemory

        mock_emb.return_value = MagicMock()
        mock_client.return_value = MagicMock()

        mock_store = MagicMock()
        mock_store.similarity_search_with_relevance_scores.return_value = []
        mock_chroma.return_value = mock_store

        ltm = LongTermMemory()
        context = ltm.format_context("不相关的查询")
        assert "未找到" in context or "知识库" in context


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
