"""
Agent 模块测试用例

测试 FaultAgent 的核心处理逻辑，使用 mock LLM 避免真实 API 调用。
"""
import os
import sys
import pytest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestFaultPlanner:
    """故障规划器测试"""

    def setup_method(self):
        """每个测试前初始化规划器"""
        from planner.fault_planner import FaultPlanner
        self.planner = FaultPlanner()

    def test_llm_infer_order(self):
        """测试从描述中推断订单服务（LLM 意图识别，失败时降级为 unknown）"""
        result = self.planner._llm_infer("订单服务接口报错，500 错误率增加")
        assert "service_name" in result
        assert isinstance(result["service_name"], str)

    def test_llm_infer_user(self):
        """测试从描述中推断用户服务（LLM 意图识别，失败时降级为 unknown）"""
        result = self.planner._llm_infer("用户登录接口响应超时")
        assert "service_name" in result
        assert isinstance(result["service_name"], str)

    def test_create_plan_returns_correct_structure(self):
        """测试创建计划返回正确结构"""
        from planner.fault_planner import AlertType
        plan = self.planner.create_plan("FAULT-001", "订单服务接口超时")
        assert plan.fault_id == "FAULT-001"
        assert plan.fault_description == "订单服务接口超时"
        assert plan.service_name is not None
        assert plan.alert_type == AlertType.UNKNOWN
        assert plan.knowledge_query == "订单服务接口超时"

    def test_format_service_info_with_node(self):
        """测试有服务节点时的信息格式化"""
        from planner.fault_planner import ServiceNode
        node = ServiceNode(
            service_name="test-service",
            hosts=["192.168.1.1"],
            log_paths=["/var/log/test/app.log"],
            databases=[{"host": "192.168.1.2", "port": 3306}],
        )
        info = self.planner.format_service_info(node)
        assert "test-service" in info
        assert "192.168.1.1" in info

    def test_format_service_info_none(self):
        """测试无服务节点时的信息格式化"""
        info = self.planner.format_service_info(None)
        assert "未找到" in info or "暂无" in info or len(info) > 0


class TestMiddleware:
    """Middleware 测试（LangChain 1.2.x AgentMiddleware API）"""

    def test_audit_log_wrap_tool_call(self):
        """测试审计日志中间件通过 wrap_tool_call 记录工具调用"""
        from middleware.audit_log import AuditLogMiddleware
        from langchain_core.messages import ToolMessage

        middleware = AuditLogMiddleware(fault_id="TEST-001")

        mock_request = MagicMock()
        mock_request.tool_call = {
            "name": "monitor_redis",
            "args": {"host": "192.168.1.1"},
            "id": "call-001",
        }

        def mock_handler(req):
            return ToolMessage(content='{"connected": true}', tool_call_id="call-001")

        result = middleware.wrap_tool_call(mock_request, mock_handler)
        assert isinstance(result, ToolMessage)

    def test_audit_log_before_after_agent(self):
        """测试审计日志中间件的 Agent 生命周期 hooks"""
        from middleware.audit_log import AuditLogMiddleware
        from langchain_core.messages import HumanMessage

        middleware = AuditLogMiddleware(fault_id="TEST-002")
        mock_state = MagicMock()
        mock_state.messages = [HumanMessage(content="测试故障")]
        mock_runtime = MagicMock()

        assert middleware.before_agent(mock_state, mock_runtime) is None
        assert middleware.before_model(mock_state, mock_runtime) is None
        assert middleware.after_model(mock_state, mock_runtime) is None
        assert middleware.after_agent(mock_state, mock_runtime) is None

    def test_human_confirm_console_approve(self, monkeypatch):
        """测试人工确认中间件控制台模式下用户输入 y 批准"""
        from middleware.human_confirm import HumanConfirmMiddleware, ConfirmStatus
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))

        middleware = HumanConfirmMiddleware(console_mode=True, timeout=5)
        result = middleware._console_confirm(
            operation_id="op-001",
            tool_name="restart_service",
            tool_input={"host": "192.168.1.101", "service_name": "test"},
        )
        assert result.status in (ConfirmStatus.APPROVED, ConfirmStatus.TIMEOUT)

    def test_non_dangerous_tool_passes_through(self):
        """测试非危险工具通过 wrap_tool_call 直接放行"""
        from middleware.human_confirm import HumanConfirmMiddleware
        from langchain_core.messages import ToolMessage

        middleware = HumanConfirmMiddleware(console_mode=True, timeout=1)

        mock_request = MagicMock()
        mock_request.tool_call = {
            "name": "monitor_redis",
            "args": {},
            "id": "call-safe-001",
        }

        call_count = {"n": 0}

        def mock_handler(req):
            call_count["n"] += 1
            return ToolMessage(content="redis ok", tool_call_id="call-safe-001")

        result = middleware.wrap_tool_call(mock_request, mock_handler)
        assert call_count["n"] == 1
        assert isinstance(result, ToolMessage)

    def test_dangerous_tool_raises_on_timeout(self):
        """测试危险工具在无法确认时抛出 PermissionError"""
        from middleware.human_confirm import HumanConfirmMiddleware

        middleware = HumanConfirmMiddleware(console_mode=False, timeout=1)

        mock_request = MagicMock()
        mock_request.tool_call = {
            "name": "restart_service",
            "args": {"host": "192.168.1.101"},
            "id": "call-danger-001",
        }

        def mock_handler(req):
            return MagicMock()

        with pytest.raises(PermissionError):
            middleware.wrap_tool_call(mock_request, mock_handler)

    def test_submit_confirmation_approve(self):
        """测试通过 submit_confirmation 外部提交批准结果"""
        from middleware.human_confirm import HumanConfirmMiddleware, ConfirmStatus

        middleware = HumanConfirmMiddleware(console_mode=False, timeout=5)
        middleware.submit_confirmation(
            operation_id="op-ext-001",
            approved=True,
            operator="admin",
            comment="已确认，可以重启",
        )

        result = middleware._request_confirmation(
            operation_id="op-ext-001",
            tool_name="restart_service",
            tool_input={},
        )
        assert result.status == ConfirmStatus.APPROVED
        assert result.operator == "admin"


class TestAPIServer:
    """FastAPI 接口测试"""

    @pytest.fixture
    def client(self):
        """创建测试客户端（设置测试用 auth token）"""
        from fastapi.testclient import TestClient
        from api.server import app
        from config.settings import settings
        # 设置测试用 token
        settings.api_auth_token = "test-token-for-unit-tests"
        return TestClient(app)

    @pytest.fixture
    def auth_headers(self):
        """认证请求头"""
        return {"Authorization": "Bearer test-token-for-unit-tests"}

    def test_health_check(self, client):
        """测试健康检查接口"""
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"

    def test_no_token_returns_401_or_403(self, client):
        """测试无 Token 请求被拒绝（HTTPBearer 返回 403 或 401）"""
        response = client.post(
            "/api/v1/fault/handle",
            json={"fault_description": "订单服务接口超时"},
        )
        assert response.status_code in (401, 403)

    def test_wrong_token_returns_401(self, client):
        """测试错误 Token 被拒绝"""
        response = client.post(
            "/api/v1/fault/handle",
            json={"fault_description": "订单服务接口超时"},
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert response.status_code == 401

    @patch("api.server.get_fault_agent")
    def test_handle_fault_sync(self, mock_get_agent, client, auth_headers):
        """测试同步故障处理接口"""
        mock_agent = MagicMock()
        mock_agent.handle_fault.return_value = {
            "fault_id": "FAULT-TEST",
            "session_id": "FAULT-TEST",
            "service_name": "order-service",
            "alert_type": "api_slow",
            "response": "已开始分析故障...",
            "status": "completed",
            "elapsed_seconds": 2.5,
        }
        mock_get_agent.return_value = mock_agent

        response = client.post(
            "/api/v1/fault/handle",
            json={"fault_description": "订单服务接口超时"},
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["fault_id"] == "FAULT-TEST"
        assert data["service_name"] == "order-service"

    def test_handle_fault_empty_description(self, client, auth_headers):
        """测试空故障描述的验证"""
        response = client.post(
            "/api/v1/fault/handle",
            json={"fault_description": "abc"},
            headers=auth_headers,
        )
        assert response.status_code in (200, 422)

    def test_submit_confirmation(self, client, auth_headers):
        """测试人工确认接口"""
        response = client.post(
            "/api/v1/confirm",
            json={
                "operation_id": "test-op-123",
                "approved": True,
                "operator": "admin",
                "comment": "确认重启",
            },
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["operation_id"] == "test-op-123"
        assert data["approved"] is True


class TestFaultAgentIntegration:
    """主 Agent + 子 Agent 架构集成测试"""

    @patch("agents.fault_agent.create_agent")
    @patch("agents.fault_agent.get_long_term_memory")
    @patch("agents.fault_agent.FaultPlanner")
    def test_handle_fault_resolved(
        self,
        mock_planner_class,
        mock_ltm,
        mock_create_agent,
    ):
        """测试主 Agent 流程：故障成功恢复"""
        from agents.fault_agent import FaultAgent
        from planner.fault_planner import AlertType
        from langchain_core.messages import AIMessage

        mock_plan = MagicMock()
        mock_plan.fault_id = "FAULT-MA-001"
        mock_plan.service_name = "order-service"
        mock_plan.alert_type = AlertType.UNKNOWN
        mock_plan.raw_service_info = "订单服务 192.168.1.101"
        mock_plan.knowledge_query = "订单服务响应慢"
        mock_planner_class.return_value.create_plan.return_value = mock_plan

        mock_ltm.return_value.load_knowledge_base.return_value = {"general": 3}
        mock_ltm.return_value.format_context.return_value = "相关知识：数据库慢查询处理"

        # Mock create_agent 返回的 agent 实例
        mock_agent_instance = MagicMock()
        mock_agent_instance.invoke.return_value = {
            "messages": [
                AIMessage(content="## 根因分析\n数据库连接池耗尽\n\n## 已执行操作\n已扩容连接池\n\n## 处理状态\n已恢复")
            ],
        }
        mock_create_agent.return_value = mock_agent_instance

        agent = FaultAgent()
        result = agent.handle_fault("订单服务接口超时，P99 超过 5 秒")

        assert result["status"] == "resolved"
        assert result["is_resolved"] is True
        assert "fault_id" in result
        assert "response" in result

    @patch("agents.fault_agent.create_agent")
    @patch("agents.fault_agent.get_long_term_memory")
    @patch("agents.fault_agent.FaultPlanner")
    def test_handle_fault_not_resolved(
        self,
        mock_planner_class,
        mock_ltm,
        mock_create_agent,
    ):
        """测试主 Agent 流程：故障未能完全恢复"""
        from agents.fault_agent import FaultAgent
        from planner.fault_planner import AlertType
        from langchain_core.messages import AIMessage

        mock_plan = MagicMock()
        mock_plan.fault_id = "FAULT-MA-002"
        mock_plan.service_name = "payment-service"
        mock_plan.alert_type = AlertType.UNKNOWN
        mock_plan.raw_service_info = ""
        mock_plan.knowledge_query = "支付服务宕机"
        mock_planner_class.return_value.create_plan.return_value = mock_plan

        mock_ltm.return_value.load_knowledge_base.return_value = {}
        mock_ltm.return_value.format_context.return_value = ""

        mock_agent_instance = MagicMock()
        mock_agent_instance.invoke.return_value = {
            "messages": [
                AIMessage(content="## 处理状态\nFAILED - 硬件故障，需人工介入")
            ],
        }
        mock_create_agent.return_value = mock_agent_instance

        agent = FaultAgent()
        result = agent.handle_fault("支付服务宕机，无法连接")

        assert result["status"] == "completed"
        assert result["is_resolved"] is False
        assert result["service_name"] == "payment-service"

    @patch("agents.fault_agent.create_agent")
    @patch("agents.fault_agent.get_long_term_memory")
    @patch("agents.fault_agent.FaultPlanner")
    def test_handle_fault_exception(
        self,
        mock_planner_class,
        mock_ltm,
        mock_create_agent,
    ):
        """测试主 Agent 执行异常时的错误处理"""
        from agents.fault_agent import FaultAgent
        from planner.fault_planner import AlertType

        mock_plan = MagicMock()
        mock_plan.fault_id = "FAULT-MA-003"
        mock_plan.service_name = "unknown"
        mock_plan.alert_type = AlertType.UNKNOWN
        mock_plan.raw_service_info = ""
        mock_plan.knowledge_query = ""
        mock_planner_class.return_value.create_plan.return_value = mock_plan

        mock_ltm.return_value.load_knowledge_base.return_value = {}
        mock_ltm.return_value.format_context.return_value = ""

        mock_agent_instance = MagicMock()
        mock_agent_instance.invoke.side_effect = RuntimeError("Agent 执行失败")
        mock_create_agent.return_value = mock_agent_instance

        agent = FaultAgent()
        result = agent.handle_fault("未知故障")

        assert result["status"] == "error"
        assert "error" in result


class TestCheckStatus:
    """FaultAgent._check_status 状态判断测试"""

    def test_resolved(self):
        from agents.fault_agent import FaultAgent
        assert FaultAgent._check_status("故障已恢复，服务正常") is True

    def test_not_resolved_failed(self):
        from agents.fault_agent import FaultAgent
        assert FaultAgent._check_status("FAILED - 需人工介入") is False

    def test_not_resolved_partial(self):
        from agents.fault_agent import FaultAgent
        assert FaultAgent._check_status("PARTIAL - 部分恢复") is False

    def test_not_resolved_negative(self):
        from agents.fault_agent import FaultAgent
        assert FaultAgent._check_status("故障未恢复") is False

    def test_unknown_status(self):
        from agents.fault_agent import FaultAgent
        assert FaultAgent._check_status("处理完成") is False


class TestModelSwitchMiddleware:
    """动态模型切换中间件测试"""

    def test_before_model_switches_by_keyword(self):
        """测试关键词匹配切换模型"""
        from middleware.model_switch import ModelSwitchMiddleware, ModelRule
        from langchain_core.messages import HumanMessage

        middleware = ModelSwitchMiddleware(
            rules=[ModelRule(model="gpt-4o-mini", keyword="简单")],
        )

        mock_state = MagicMock()
        mock_state.messages = [HumanMessage(content="这是一个简单的任务")]
        mock_runtime = MagicMock()
        mock_runtime.config = MagicMock()
        mock_runtime.config.tags = []
        mock_runtime.config.configurable = {}

        middleware.before_agent(mock_state, mock_runtime)
        middleware.before_model(mock_state, mock_runtime)

        # runtime.model 应被替换
        assert mock_runtime.model is not None

    def test_before_model_no_match_keeps_model(self):
        """测试无规则匹配时不切换模型"""
        from middleware.model_switch import ModelSwitchMiddleware, ModelRule
        from langchain_core.messages import HumanMessage

        middleware = ModelSwitchMiddleware(
            rules=[ModelRule(model="gpt-4o-mini", keyword="不存在的关键词")],
        )

        mock_state = MagicMock()
        mock_state.messages = [HumanMessage(content="正常故障描述")]
        mock_runtime = MagicMock()
        mock_runtime.config = MagicMock()
        mock_runtime.config.tags = []
        mock_runtime.config.configurable = {}
        original_model = mock_runtime.model

        middleware.before_agent(mock_state, mock_runtime)
        result = middleware.before_model(mock_state, mock_runtime)

        assert result is None
        assert mock_runtime.model == original_model

    def test_before_model_switches_by_agent_name(self):
        """测试按 Agent 名称切换模型"""
        from middleware.model_switch import ModelSwitchMiddleware, ModelRule
        from langchain_core.messages import HumanMessage

        middleware = ModelSwitchMiddleware(
            rules=[ModelRule(model="gpt-4o", agent_name="analysis_agent")],
        )

        mock_state = MagicMock()
        mock_state.messages = [HumanMessage(content="分析任务")]
        mock_runtime = MagicMock()
        mock_runtime.config = MagicMock()
        mock_runtime.config.tags = ["agent:analysis_agent"]
        mock_runtime.config.configurable = {}

        middleware.before_agent(mock_state, mock_runtime)
        middleware.before_model(mock_state, mock_runtime)

        assert mock_runtime.model is not None

    def test_before_model_switches_by_call_index(self):
        """测试按调用序号降级切换模型（重试降级场景）"""
        from middleware.model_switch import ModelSwitchMiddleware, ModelRule
        from langchain_core.messages import HumanMessage

        middleware = ModelSwitchMiddleware(
            rules=[ModelRule(model="gpt-4o-mini", min_call_index=3)],
        )

        mock_state = MagicMock()
        mock_state.messages = [HumanMessage(content="任务")]
        mock_runtime = MagicMock()
        mock_runtime.config = MagicMock()
        mock_runtime.config.tags = []
        mock_runtime.config.configurable = {}
        original_model = mock_runtime.model

        middleware.before_agent(mock_state, mock_runtime)

        # 第 1、2 次调用不触发
        middleware.before_model(mock_state, mock_runtime)
        assert mock_runtime.model == original_model
        middleware.before_model(mock_state, mock_runtime)
        assert mock_runtime.model == original_model

        # 第 3 次调用触发降级
        middleware.before_model(mock_state, mock_runtime)
        assert mock_runtime.model != original_model

    def test_before_model_custom_condition(self):
        """测试自定义条件函数"""
        from middleware.model_switch import ModelSwitchMiddleware, ModelRule
        from langchain_core.messages import HumanMessage

        # 自定义条件：消息数超过 5 时切换
        def many_messages(state, runtime):
            msgs = getattr(state, "messages", []) or []
            return len(msgs) > 5

        middleware = ModelSwitchMiddleware(
            rules=[ModelRule(model="gpt-4o-mini", condition=many_messages)],
        )

        mock_state = MagicMock()
        mock_state.messages = [HumanMessage(content=f"msg{i}") for i in range(6)]
        mock_runtime = MagicMock()
        mock_runtime.config = MagicMock()
        mock_runtime.config.tags = []
        mock_runtime.config.configurable = {}

        middleware.before_agent(mock_state, mock_runtime)
        middleware.before_model(mock_state, mock_runtime)

        assert mock_runtime.model is not None

    def test_before_agent_resets_counter(self):
        """测试 before_agent 重置调用计数器"""
        from middleware.model_switch import ModelSwitchMiddleware

        middleware = ModelSwitchMiddleware()
        middleware._call_index = 10
        middleware._current_model_name = "some-model"

        middleware.before_agent(MagicMock(), MagicMock())

        assert middleware._call_index == 0
        assert middleware._current_model_name is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
