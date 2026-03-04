"""
工具模块测试用例

测试各工具的输入验证、返回格式和错误处理。
使用 mock 对象替代真实的网络/SSH 连接。
"""
import json
import sys
import os
import pytest
from unittest.mock import MagicMock, patch, PropertyMock

# 将项目根目录加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestProcessMonitorTool:
    """进程监控工具测试"""

    def test_tool_name_and_description(self):
        """测试工具名称和描述"""
        from tools.process_monitor import ProcessMonitorTool
        tool = ProcessMonitorTool()
        assert tool.name == "monitor_process"
        assert len(tool.description) > 10

    @patch("tools.process_monitor.paramiko.SSHClient")
    def test_run_process_running(self, mock_ssh_class):
        """测试进程运行时的监控结果"""
        from tools.process_monitor import ProcessMonitorTool

        # 模拟 SSH 连接和命令执行
        mock_ssh = MagicMock()
        mock_ssh_class.return_value = mock_ssh

        # 模拟 pgrep 输出（进程存在）
        mock_stdout1 = MagicMock()
        mock_stdout1.read.return_value = b"1234 /usr/bin/java -jar order-service.jar"

        # 模拟 ps aux 输出
        mock_stdout2 = MagicMock()
        mock_stdout2.read.return_value = b"cpu=15.2% mem=8.5% count=2"

        # 模拟 uptime 和 free 输出
        mock_stdout3 = MagicMock()
        mock_stdout3.read.return_value = b"load average: 0.5, 0.4, 0.3\nmem_total=8192MB mem_used=4096MB mem_free=4096MB"

        mock_ssh.exec_command.side_effect = [
            (None, mock_stdout1, MagicMock()),
            (None, mock_stdout2, MagicMock()),
            (None, mock_stdout3, MagicMock()),
        ]

        tool = ProcessMonitorTool()
        result_str = tool._run(host="192.168.1.101", service_name="order-service")
        result = json.loads(result_str)

        assert result["success"] is True
        assert result["tool"] == "monitor_process"
        assert result["data"]["process_running"] is True
        assert result["data"]["host"] == "192.168.1.101"

    @patch("tools.process_monitor.paramiko.SSHClient")
    def test_run_process_not_running(self, mock_ssh_class):
        """测试进程不存在时的监控结果"""
        from tools.process_monitor import ProcessMonitorTool

        mock_ssh = MagicMock()
        mock_ssh_class.return_value = mock_ssh

        # 模拟 pgrep 返回空（进程不存在）
        mock_stdout1 = MagicMock()
        mock_stdout1.read.return_value = b""

        mock_stdout3 = MagicMock()
        mock_stdout3.read.return_value = b"load average: 0.1\nmem_total=8192MB mem_used=2048MB mem_free=6144MB"

        mock_ssh.exec_command.side_effect = [
            (None, mock_stdout1, MagicMock()),
            (None, mock_stdout3, MagicMock()),
        ]

        tool = ProcessMonitorTool()
        result_str = tool._run(host="192.168.1.101", service_name="order-service")
        result = json.loads(result_str)

        assert result["success"] is True
        assert result["data"]["process_running"] is False

    @patch("tools.process_monitor.paramiko.SSHClient")
    def test_ssh_connection_failure(self, mock_ssh_class):
        """测试 SSH 连接失败时的错误处理"""
        from tools.process_monitor import ProcessMonitorTool

        mock_ssh = MagicMock()
        mock_ssh_class.return_value = mock_ssh
        mock_ssh.connect.side_effect = Exception("Connection refused")

        tool = ProcessMonitorTool()
        result_str = tool._run(host="192.168.1.999", service_name="test-service")
        result = json.loads(result_str)

        assert result["success"] is False
        assert "error" in result


class TestRedisMonitorTool:
    """Redis 监控工具测试"""

    def test_tool_attributes(self):
        """测试工具属性"""
        from tools.redis_monitor import RedisMonitorTool
        tool = RedisMonitorTool()
        assert tool.name == "monitor_redis"
        assert "Redis" in tool.description

    @patch("tools.redis_monitor.redis.Redis")
    def test_redis_normal_state(self, mock_redis_class):
        """测试 Redis 正常状态的监控"""
        from tools.redis_monitor import RedisMonitorTool

        mock_redis = MagicMock()
        mock_redis_class.return_value = mock_redis
        mock_redis.ping.return_value = True
        mock_redis.info.return_value = {
            "redis_version": "7.0.0",
            "uptime_in_days": 30,
            "used_memory_human": "512.00M",
            "used_memory_peak_human": "600.00M",
            "maxmemory_human": "4.00G",
            "mem_fragmentation_ratio": 1.2,
            "connected_clients": 50,
            "blocked_clients": 0,
            "rejected_connections": 0,
            "total_commands_processed": 1000000,
            "instantaneous_ops_per_sec": 500,
            "total_net_input_bytes": 100000000,
            "total_net_output_bytes": 200000000,
            "keyspace_hits": 900000,
            "keyspace_misses": 100000,
            "evicted_keys": 0,
            "expired_keys": 50000,
            "role": "master",
            "keyspace": {"db0": {"keys": 100000}},
        }
        mock_redis.slowlog_get.return_value = []
        mock_redis.dbsize.return_value = 100000

        tool = RedisMonitorTool()
        result_str = tool._run(host="192.168.1.150", port=6379)
        result = json.loads(result_str)

        assert result["success"] is True
        assert result["data"]["connected"] is True
        assert result["data"]["server_version"] == "7.0.0"
        assert result["data"]["total_keys"] == 100000

    @patch("tools.redis_monitor.redis.Redis")
    def test_redis_connection_failure(self, mock_redis_class):
        """测试 Redis 连接失败"""
        from tools.redis_monitor import RedisMonitorTool

        mock_redis = MagicMock()
        mock_redis_class.return_value = mock_redis
        mock_redis.ping.side_effect = ConnectionError("Connection refused")

        tool = RedisMonitorTool()
        result_str = tool._run(host="192.168.1.999")
        result = json.loads(result_str)

        assert result["success"] is False
        assert "error" in result


class TestLogAnalyzerTool:
    """日志分析工具测试"""

    @patch("tools.log_analyzer.paramiko.SSHClient")
    def test_analyze_logs_with_errors(self, mock_ssh_class):
        """测试有错误的日志分析"""
        from tools.log_analyzer import LogAnalyzerTool

        mock_ssh = MagicMock()
        mock_ssh_class.return_value = mock_ssh

        sample_log = (
            "2024-01-15 14:30:00 INFO Starting order service\n"
            "2024-01-15 14:30:01 ERROR Connection refused to database\n"
            "2024-01-15 14:30:02 ERROR java.lang.NullPointerException at OrderService.java:125\n"
            "2024-01-15 14:30:03 WARN Redis connection timeout after 3000ms\n"
            "2024-01-15 14:30:04 ERROR Unable to acquire JDBC Connection\n"
            "2024-01-15 14:30:05 ERROR java.lang.NullPointerException at OrderService.java:125\n"
        )

        mock_stdout_log = MagicMock()
        mock_stdout_log.read.return_value = sample_log.encode()

        mock_stdout_stat = MagicMock()
        mock_stdout_stat.read.return_value = b"2024-01-15 14:30:00"

        mock_stdout_du = MagicMock()
        mock_stdout_du.read.return_value = b"15M\t/var/log/order-service/app.log"

        mock_ssh.exec_command.side_effect = [
            (None, mock_stdout_log, MagicMock()),
            (None, mock_stdout_stat, MagicMock()),
            (None, mock_stdout_du, MagicMock()),
        ]

        tool = LogAnalyzerTool()
        result_str = tool._run(
            host="192.168.1.101",
            log_path="/var/log/order-service/app.log",
        )
        result = json.loads(result_str)

        assert result["success"] is True
        assert result["data"]["has_errors"] is True
        assert result["data"]["total_lines_analyzed"] > 0
        assert len(result["data"]["error_summary"]) > 0

    @patch("tools.log_analyzer.paramiko.SSHClient")
    def test_file_not_found(self, mock_ssh_class):
        """测试日志文件不存在的情况"""
        from tools.log_analyzer import LogAnalyzerTool

        mock_ssh = MagicMock()
        mock_ssh_class.return_value = mock_ssh

        mock_stdout = MagicMock()
        mock_stdout.read.return_value = b"FILE_NOT_FOUND"

        mock_ssh.exec_command.return_value = (None, mock_stdout, MagicMock())

        tool = LogAnalyzerTool()
        result_str = tool._run(host="192.168.1.101", log_path="/nonexistent/app.log")
        result = json.loads(result_str)

        assert result["success"] is True  # 工具执行成功，但文件不存在
        assert result["data"]["status"] == "file_not_found"


class TestNotificationTool:
    """通知工具测试"""

    def test_tool_attributes(self):
        """测试工具属性"""
        from tools.notification import NotificationTool
        tool = NotificationTool()
        assert tool.name == "send_notification"
        assert len(tool.description) > 10

    @patch("tools.notification.httpx.Client")
    def test_dingtalk_notification_success(self, mock_client_class):
        """测试钉钉通知成功"""
        from tools.notification import NotificationTool
        from config.settings import settings

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"errcode": 0, "errmsg": "ok"}
        mock_resp.raise_for_status.return_value = None

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp
        mock_client_class.return_value = mock_client

        # 临时设置 dingtalk webhook
        original = settings.notify_dingtalk_webhook
        settings.notify_dingtalk_webhook = "https://oapi.dingtalk.com/robot/send?access_token=test"

        tool = NotificationTool()
        result_str = tool._run(
            message="订单服务已恢复正常",
            title="WeOps 恢复通知",
            severity="recovery",
            channels=["dingtalk"],
        )
        result = json.loads(result_str)

        settings.notify_dingtalk_webhook = original

        assert result["success"] is True
        assert "dingtalk" in result["data"]["channels"]

    def test_no_channels_configured(self):
        """测试无通知渠道时的处理"""
        from tools.notification import NotificationTool
        from config.settings import settings

        # 临时清空所有通知渠道
        original_dd = settings.notify_dingtalk_webhook
        original_slack = settings.notify_slack_webhook
        original_smtp = settings.notify_email_smtp_host
        settings.notify_dingtalk_webhook = None
        settings.notify_slack_webhook = None
        settings.notify_email_smtp_host = None

        tool = NotificationTool()
        result_str = tool._run(message="test", channels=None)
        result = json.loads(result_str)

        # 恢复配置
        settings.notify_dingtalk_webhook = original_dd
        settings.notify_slack_webhook = original_slack
        settings.notify_email_smtp_host = original_smtp

        assert result["success"] is False


class TestServiceRestartTool:
    """服务重启工具测试"""

    def test_blacklist_protection(self):
        """测试黑名单保护机制"""
        from tools.service_restart import ServiceRestartTool
        from config.settings import settings

        # 设置黑名单
        original = settings.restart_blacklist_hosts
        settings.restart_blacklist_hosts = "192.168.1.100,192.168.1.200"

        tool = ServiceRestartTool()
        result_str = tool._run(host="192.168.1.100", service_name="critical-service")
        result = json.loads(result_str)

        settings.restart_blacklist_hosts = original

        assert result["success"] is False
        assert "黑名单" in result["error"]

    def test_tool_name(self):
        """测试工具名称（用于危险操作识别）"""
        from tools.service_restart import ServiceRestartTool
        tool = ServiceRestartTool()
        # 必须与 DANGEROUS_TOOLS 中的名称一致
        assert tool.name == "restart_service"


class TestToolBase:
    """工具基础功能测试"""

    def test_format_tool_result_success(self):
        """测试成功结果格式化"""
        from tools.base import format_tool_result
        result = format_tool_result("test_tool", True, data={"key": "value"}, elapsed=1.5)
        assert result["tool"] == "test_tool"
        assert result["success"] is True
        assert result["data"] == {"key": "value"}
        assert result["elapsed_seconds"] == 1.5
        assert "timestamp" in result

    def test_format_tool_result_failure(self):
        """测试失败结果格式化"""
        from tools.base import format_tool_result
        result = format_tool_result("test_tool", False, error="connection refused")
        assert result["success"] is False
        assert result["error"] == "connection refused"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
