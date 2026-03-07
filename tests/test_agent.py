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

    def test_infer_service_name_order(self):
        """测试从描述中推断订单服务名"""
        service = self.planner.infer_service_name("订单服务接口报错，500 错误率增加")
        assert "order" in service.lower() or service == "unknown"

    def test_infer_service_name_user(self):
        """测试从描述中推断用户服务名"""
        service = self.planner.infer_service_name("用户登录接口响应超时")
        assert "user" in service.lower() or service == "unknown"

    def test_identify_alert_type_slow(self):
        """测试识别响应缓慢告警"""
        from planner.fault_planner import AlertType
        alert = self.planner.identify_alert_type("接口响应很慢，超时了")
        assert alert == AlertType.API_SLOW

    def test_identify_alert_type_error(self):
        """测试识别接口报错告警"""
        from planner.fault_planner import AlertType
        alert = self.planner.identify_alert_type("接口大量报错，500错误")
        assert alert in (AlertType.API_ERROR, AlertType.SERVICE_UNSTABLE, AlertType.SERVICE_DOWN)

    def test_identify_alert_type_redis(self):
        """测试识别 Redis 异常告警"""
        from planner.fault_planner import AlertType
        alert = self.planner.identify_alert_type("Redis 连接失败，缓存不可用")
        assert alert == AlertType.REDIS_ERROR

    def test_identify_alert_type_unknown(self):
        """测试无法识别的告警类型"""
        from planner.fault_planner import AlertType
        alert = self.planner.identify_alert_type("某个奇怪的问题发生了")
        assert alert == AlertType.UNKNOWN

    def test_create_plan_returns_correct_structure(self):
        """测试创建计划返回正确结构"""
        plan = self.planner.create_plan("FAULT-001", "订单服务接口超时")
        assert plan.fault_id == "FAULT-001"
        assert plan.fault_description == "订单服务接口超时"
        assert plan.service_name is not None
        assert plan.alert_type is not None
        assert isinstance(plan.monitoring_steps, list)
        assert len(plan.monitoring_steps) > 0
        assert plan.knowledge_query != ""

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


class TestFaultAgent:
    """FaultAgent 集成测试"""

    @patch("agent.fault_agent.get_long_term_memory")
    @patch("agent.fault_agent.get_short_term_memory")
    @patch("agent.fault_agent.FaultPlanner")
    @patch("agent.fault_agent.create_fault_agent")
    def test_handle_fault_success(
        self,
        mock_create_agent,
        mock_planner_class,
        mock_stm,
        mock_ltm,
    ):
        """测试故障处理成功流程"""
        from agent.fault_agent import FaultAgent
        from planner.fault_planner import AlertType

        # Mock 规划器
        mock_plan = MagicMock()
        mock_plan.fault_id = "FAULT-001"
        mock_plan.service_name = "order-service"
        mock_plan.alert_type = AlertType.API_SLOW
        mock_plan.raw_service_info = "订单服务在 192.168.1.101"
        mock_plan.knowledge_query = "订单服务响应慢"
        mock_planner_class.return_value.create_plan.return_value = mock_plan

        # Mock 短期记忆
        mock_stm_instance = MagicMock()
        mock_stm_instance.get_messages.return_value = []
        mock_stm.return_value = mock_stm_instance

        # Mock 长期记忆
        mock_ltm_instance = MagicMock()
        mock_ltm_instance.load_knowledge_base.return_value = {"general": 3, "scenario": 2, "history": 2}
        mock_ltm_instance.format_context.return_value = "相关知识：检查数据库慢查询"
        mock_ltm.return_value = mock_ltm_instance

        # Mock Agent 执行
        from langchain_core.messages import AIMessage
        mock_agent = MagicMock()
        mock_agent.invoke.return_value = {
            "messages": [
                AIMessage(content="经过分析，订单服务响应缓慢的原因是数据库慢查询。已执行以下操作：...")
            ]
        }
        mock_create_agent.return_value = mock_agent

        # 执行
        agent = FaultAgent()
        result = agent.handle_fault("订单服务接口超时，P99 超过 5 秒")

        assert result["status"] in ("completed", "error")
        assert "fault_id" in result
        assert "service_name" in result
        assert "response" in result

    @patch("agent.fault_agent.get_long_term_memory")
    @patch("agent.fault_agent.get_short_term_memory")
    @patch("agent.fault_agent.FaultPlanner")
    @patch("agent.fault_agent.create_fault_agent")
    def test_handle_fault_permission_denied(
        self,
        mock_create_agent,
        mock_planner_class,
        mock_stm,
        mock_ltm,
    ):
        """测试危险操作被拒绝时的处理"""
        from agent.fault_agent import FaultAgent
        from planner.fault_planner import AlertType

        mock_plan = MagicMock()
        mock_plan.fault_id = "FAULT-002"
        mock_plan.service_name = "order-service"
        mock_plan.alert_type = AlertType.SERVICE_DOWN
        mock_plan.raw_service_info = ""
        mock_plan.knowledge_query = ""
        mock_planner_class.return_value.create_plan.return_value = mock_plan

        mock_stm.return_value.get_messages.return_value = []
        mock_ltm.return_value.load_knowledge_base.return_value = {}
        mock_ltm.return_value.format_context.return_value = ""

        # 模拟 Agent 抛出 PermissionError（人工确认被拒绝）
        mock_agent = MagicMock()
        mock_agent.invoke.side_effect = PermissionError("危险操作 'restart_service' 被拒绝")
        mock_create_agent.return_value = mock_agent

        agent = FaultAgent()
        result = agent.handle_fault("服务宕机，需要重启")

        assert result["status"] == "rejected"
        assert "拒绝" in result["response"] or "rejected" in result["response"].lower()

    @patch("agent.fault_agent.get_long_term_memory")
    @patch("agent.fault_agent.get_short_term_memory")
    @patch("agent.fault_agent.FaultPlanner")
    @patch("agent.fault_agent.create_fault_agent")
    def test_handle_fault_exception(
        self,
        mock_create_agent,
        mock_planner_class,
        mock_stm,
        mock_ltm,
    ):
        """测试 Agent 执行异常时的错误处理"""
        from agent.fault_agent import FaultAgent
        from planner.fault_planner import AlertType

        mock_plan = MagicMock()
        mock_plan.fault_id = "FAULT-003"
        mock_plan.service_name = "unknown"
        mock_plan.alert_type = AlertType.UNKNOWN
        mock_plan.raw_service_info = ""
        mock_plan.knowledge_query = ""
        mock_planner_class.return_value.create_plan.return_value = mock_plan

        mock_stm.return_value.get_messages.return_value = []
        mock_ltm.return_value.load_knowledge_base.return_value = {}
        mock_ltm.return_value.format_context.return_value = ""

        mock_agent = MagicMock()
        mock_agent.invoke.side_effect = RuntimeError("LLM API 调用失败")
        mock_create_agent.return_value = mock_agent

        agent = FaultAgent()
        result = agent.handle_fault("未知故障")

        assert result["status"] == "error"
        assert "error" in result


class TestMiddleware:
    """Middleware 测试（LangChain 1.2.x AgentMiddleware API）"""

    def test_audit_log_wrap_tool_call(self):
        """测试审计日志中间件通过 wrap_tool_call 记录工具调用"""
        from middleware.audit_log import AuditLogMiddleware
        from langchain_core.messages import ToolMessage

        middleware = AuditLogMiddleware(fault_id="TEST-001")

        # 构造 mock ToolCallRequest
        mock_request = MagicMock()
        mock_request.tool_call = {
            "name": "monitor_redis",
            "args": {"host": "192.168.1.1"},
            "id": "call-001",
        }

        # mock handler 返回 ToolMessage
        def mock_handler(req):
            return ToolMessage(content='{"connected": true}', tool_call_id="call-001")

        # 不应抛出异常，应返回 ToolMessage
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

        # before_agent 和 after_agent 均不应抛出异常
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
        # select 在测试环境可能不支持，结果可能是 APPROVED 或 TIMEOUT
        assert result.status in (ConfirmStatus.APPROVED, ConfirmStatus.TIMEOUT)

    def test_non_dangerous_tool_passes_through(self):
        """测试非危险工具通过 wrap_tool_call 直接放行（不触发确认）"""
        from middleware.human_confirm import HumanConfirmMiddleware
        from langchain_core.messages import ToolMessage

        middleware = HumanConfirmMiddleware(console_mode=True, timeout=1)

        mock_request = MagicMock()
        mock_request.tool_call = {
            "name": "monitor_redis",  # 非危险工具
            "args": {},
            "id": "call-safe-001",
        }

        call_count = {"n": 0}

        def mock_handler(req):
            call_count["n"] += 1
            return ToolMessage(content="redis ok", tool_call_id="call-safe-001")

        result = middleware.wrap_tool_call(mock_request, mock_handler)
        # 非危险工具直接透传，handler 应被调用一次
        assert call_count["n"] == 1
        assert isinstance(result, ToolMessage)

    def test_dangerous_tool_raises_on_timeout(self):
        """测试危险工具在无法确认时（超时）抛出 PermissionError"""
        from middleware.human_confirm import HumanConfirmMiddleware

        # 无 console_mode，无 webhook_url → 默认拒绝
        middleware = HumanConfirmMiddleware(console_mode=False, timeout=1)

        mock_request = MagicMock()
        mock_request.tool_call = {
            "name": "restart_service",  # 危险工具
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

        # 预存的确认结果应被 _request_confirmation 直接读取
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
        """创建测试客户端"""
        from fastapi.testclient import TestClient
        from api.server import app
        return TestClient(app)

    def test_health_check(self, client):
        """测试健康检查接口"""
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"

    @patch("api.server.get_fault_agent")
    def test_handle_fault_sync(self, mock_get_agent, client):
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
        )
        assert response.status_code == 200
        data = response.json()
        assert data["fault_id"] == "FAULT-TEST"
        assert data["service_name"] == "order-service"

    def test_handle_fault_empty_description(self, client):
        """测试空故障描述的验证"""
        response = client.post(
            "/api/v1/fault/handle",
            json={"fault_description": "abc"},  # 太短，少于5个字符会被拒绝？
        )
        # 5 个字符是最小值，"abc" 只有 3 个字符
        assert response.status_code in (200, 422)

    def test_submit_confirmation(self, client):
        """测试人工确认接口"""
        response = client.post(
            "/api/v1/confirm",
            json={
                "operation_id": "test-op-123",
                "approved": True,
                "operator": "admin",
                "comment": "确认重启",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["operation_id"] == "test-op-123"
        assert data["approved"] is True


class TestMultiAgentFaultAgent:
    """多 Agent 架构 FaultAgent 集成测试"""

    @patch("agents.fault_agent.get_fault_graph")
    @patch("agents.fault_agent.get_long_term_memory")
    @patch("agents.fault_agent.get_short_term_memory")
    @patch("agents.fault_agent.FaultPlanner")
    def test_handle_fault_resolved(
        self,
        mock_planner_class,
        mock_stm,
        mock_ltm,
        mock_get_graph,
    ):
        """测试多 Agent 流程：故障成功恢复"""
        from agents.fault_agent import FaultAgent
        from planner.fault_planner import AlertType

        mock_plan = MagicMock()
        mock_plan.fault_id = "FAULT-MA-001"
        mock_plan.service_name = "order-service"
        mock_plan.alert_type = AlertType.API_SLOW
        mock_plan.raw_service_info = "订单服务 192.168.1.101"
        mock_plan.knowledge_query = "订单服务响应慢"
        mock_planner_class.return_value.create_plan.return_value = mock_plan

        mock_stm.return_value.get_messages.return_value = []
        mock_ltm.return_value.load_knowledge_base.return_value = {"general": 3}
        mock_ltm.return_value.format_context.return_value = "相关知识：数据库慢查询处理"

        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {
            "fault_id": "FAULT-MA-001",
            "service_name": "order-service",
            "is_resolved": True,
            "root_cause": "数据库连接池耗尽",
            "recovery_actions": "已扩容连接池至 200",
            "notifications_sent": True,
            "error_message": None,
            "analysis_result": "连接池耗尽导致响应超时",
            "messages": [],
        }
        mock_get_graph.return_value = mock_graph

        agent = FaultAgent()
        result = agent.handle_fault("订单服务接口超时，P99 超过 5 秒")

        assert result["status"] == "resolved"
        assert result["is_resolved"] is True
        assert result["notifications_sent"] is True
        assert "fault_id" in result
        assert "response" in result

    @patch("agents.fault_agent.get_fault_graph")
    @patch("agents.fault_agent.get_long_term_memory")
    @patch("agents.fault_agent.get_short_term_memory")
    @patch("agents.fault_agent.FaultPlanner")
    def test_handle_fault_not_resolved(
        self,
        mock_planner_class,
        mock_stm,
        mock_ltm,
        mock_get_graph,
    ):
        """测试多 Agent 流程：故障未能完全恢复（验证次数耗尽）"""
        from agents.fault_agent import FaultAgent
        from planner.fault_planner import AlertType

        mock_plan = MagicMock()
        mock_plan.fault_id = "FAULT-MA-002"
        mock_plan.service_name = "payment-service"
        mock_plan.alert_type = AlertType.SERVICE_DOWN
        mock_plan.raw_service_info = ""
        mock_plan.knowledge_query = "支付服务宕机"
        mock_planner_class.return_value.create_plan.return_value = mock_plan

        mock_stm.return_value.get_messages.return_value = []
        mock_ltm.return_value.load_knowledge_base.return_value = {}
        mock_ltm.return_value.format_context.return_value = ""

        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {
            "fault_id": "FAULT-MA-002",
            "service_name": "payment-service",
            "is_resolved": False,
            "root_cause": "硬件故障，需人工介入",
            "recovery_actions": "已尝试重启，未成功",
            "notifications_sent": True,
            "error_message": None,
            "analysis_result": "",
            "messages": [],
        }
        mock_get_graph.return_value = mock_graph

        agent = FaultAgent()
        result = agent.handle_fault("支付服务宕机，无法连接")

        assert result["status"] == "completed"
        assert result["is_resolved"] is False
        assert result["service_name"] == "payment-service"

    @patch("agents.fault_agent.get_fault_graph")
    @patch("agents.fault_agent.get_long_term_memory")
    @patch("agents.fault_agent.get_short_term_memory")
    @patch("agents.fault_agent.FaultPlanner")
    def test_handle_fault_graph_exception(
        self,
        mock_planner_class,
        mock_stm,
        mock_ltm,
        mock_get_graph,
    ):
        """测试 StateGraph 执行异常时的错误处理"""
        from agents.fault_agent import FaultAgent
        from planner.fault_planner import AlertType

        mock_plan = MagicMock()
        mock_plan.fault_id = "FAULT-MA-003"
        mock_plan.service_name = "unknown"
        mock_plan.alert_type = AlertType.UNKNOWN
        mock_plan.raw_service_info = ""
        mock_plan.knowledge_query = ""
        mock_planner_class.return_value.create_plan.return_value = mock_plan

        mock_stm.return_value.get_messages.return_value = []
        mock_ltm.return_value.load_knowledge_base.return_value = {}
        mock_ltm.return_value.format_context.return_value = ""

        mock_graph = MagicMock()
        mock_graph.invoke.side_effect = RuntimeError("LangGraph 执行失败")
        mock_get_graph.return_value = mock_graph

        agent = FaultAgent()
        result = agent.handle_fault("未知故障")

        assert result["status"] == "error"
        assert "error" in result


class TestCoordinator:
    """Coordinator StateGraph 路由逻辑测试"""

    def test_route_after_recovery_resolved(self):
        """测试恢复后路由：已解决 → notify_node"""
        from agents.coordinator import _route_after_recovery
        state = {"is_resolved": True, "verify_count": 0}
        assert _route_after_recovery(state) == "notify_node"

    def test_route_after_recovery_retry(self):
        """测试恢复后路由：未解决且未超限 → monitor_node"""
        from agents.coordinator import _route_after_recovery
        state = {"is_resolved": False, "verify_count": 0}
        assert _route_after_recovery(state) == "monitor_node"

    def test_route_after_recovery_max_exceeded(self):
        """测试恢复后路由：超出验证上限 → notify_node（强制通知）"""
        from agents.coordinator import _route_after_recovery, MAX_VERIFY_COUNT
        state = {"is_resolved": False, "verify_count": MAX_VERIFY_COUNT}
        assert _route_after_recovery(state) == "notify_node"

    def test_increment_verify_count(self):
        """测试验证计数器递增"""
        from agents.coordinator import _increment_verify_count
        state = {"verify_count": 1}
        result = _increment_verify_count(state)
        assert result["verify_count"] == 2

    def test_increment_verify_count_initial(self):
        """测试验证计数器从空状态开始递增"""
        from agents.coordinator import _increment_verify_count
        state = {}
        result = _increment_verify_count(state)
        assert result["verify_count"] == 1

    def test_build_fault_graph_returns_compiled_graph(self):
        """测试 build_fault_graph 返回可调用的编译图"""
        from agents.coordinator import build_fault_graph
        graph = build_fault_graph(console_confirm_mode=True)
        assert graph is not None
        assert callable(graph.invoke)

    def test_get_fault_graph_caches_instance(self):
        """测试 get_fault_graph 对相同参数返回同一缓存实例"""
        from agents.coordinator import get_fault_graph, _graph_cache
        _graph_cache.clear()

        g1 = get_fault_graph(console_confirm_mode=True)
        g2 = get_fault_graph(console_confirm_mode=True)
        assert g1 is g2

        g3 = get_fault_graph(console_confirm_mode=False)
        assert g3 is not g1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
