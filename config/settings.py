"""
配置模块 - 使用 pydantic-settings 管理所有环境变量和配置
"""
import os
from pathlib import Path
from typing import List, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent


class Settings(BaseSettings):
    """全局配置类，从环境变量或 .env 文件加载"""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ===== LLM =====
    openai_api_key: str = Field(default="", description="OpenAI API Key")
    openai_api_base: str = Field(default="https://api.openai.com/v1", description="OpenAI API Base URL")
    openai_model: str = Field(default="gpt-4o", description="OpenAI 模型名称")
    openai_temperature: float = Field(default=0.1, description="LLM Temperature")
    openai_max_tokens: int = Field(default=4096, description="最大 Token 数")

    # ===== Vector DB =====
    chroma_persist_dir: str = Field(default="./chroma_db", description="ChromaDB 持久化目录")
    chroma_collection_general: str = Field(default="general_measures", description="通用措施集合名")
    chroma_collection_scenarios: str = Field(default="scenario_measures", description="场景措施集合名")
    chroma_collection_history: str = Field(default="history_measures", description="历史措施集合名")

    # ===== Embedding =====
    embedding_model: str = Field(default="text-embedding-ada-002", description="Embedding 模型名")

    # ===== API Server =====
    api_host: str = Field(default="0.0.0.0", description="API 监听地址")
    api_port: int = Field(default=8080, description="API 监听端口")
    api_debug: bool = Field(default=False, description="调试模式")

    # ===== Memory =====
    memory_max_tokens: int = Field(default=4000, description="短期记忆最大 Token 数")
    memory_window_size: int = Field(default=20, description="对话窗口大小")

    # ===== Human Confirmation =====
    human_confirm_timeout: int = Field(default=300, description="人工确认超时秒数")
    human_confirm_webhook_url: Optional[str] = Field(default=None, description="人工确认 Webhook URL")

    # ===== Notification =====
    notify_dingtalk_webhook: Optional[str] = Field(default=None, description="钉钉 Webhook")
    notify_slack_webhook: Optional[str] = Field(default=None, description="Slack Webhook")
    notify_email_smtp_host: Optional[str] = Field(default=None)
    notify_email_smtp_port: int = Field(default=465)
    notify_email_user: Optional[str] = Field(default=None)
    notify_email_password: Optional[str] = Field(default=None)
    notify_email_recipients: str = Field(default="", description="逗号分隔的收件人邮箱")

    # ===== Redis Monitor =====
    monitor_redis_host: str = Field(default="127.0.0.1")
    monitor_redis_port: int = Field(default=6379)
    monitor_redis_password: Optional[str] = Field(default=None)
    monitor_redis_db: int = Field(default=0)

    # ===== DB Monitor =====
    monitor_db_host: str = Field(default="127.0.0.1")
    monitor_db_port: int = Field(default=3306)
    monitor_db_user: str = Field(default="readonly_user")
    monitor_db_password: Optional[str] = Field(default=None)
    monitor_db_name: str = Field(default="production")

    # ===== MQ Monitor =====
    monitor_mq_host: str = Field(default="127.0.0.1")
    monitor_mq_port: int = Field(default=5672)
    monitor_mq_user: str = Field(default="guest")
    monitor_mq_password: str = Field(default="guest")
    monitor_mq_management_port: int = Field(default=15672)

    # ===== SSH =====
    ssh_default_user: str = Field(default="ops")
    ssh_default_key_path: str = Field(default="~/.ssh/id_rsa")
    ssh_default_port: int = Field(default=22)

    # ===== Tool Safety =====
    restart_blacklist_hosts: str = Field(default="", description="禁止重启的主机，逗号分隔")
    tool_max_retries: int = Field(default=3)
    tool_retry_wait_seconds: float = Field(default=2.0)

    # ===== Logging =====
    log_level: str = Field(default="INFO")
    log_file: str = Field(default="./logs/weops_agent.log")
    audit_log_file: str = Field(default="./logs/audit.log")

    # ===== Data Directories =====
    @property
    def data_dir(self) -> Path:
        return PROJECT_ROOT / "data"

    @property
    def service_node_dir(self) -> Path:
        return PROJECT_ROOT / "service_node"

    @property
    def prompts_dir(self) -> Path:
        return PROJECT_ROOT / "prompts" / "templates"

    @property
    def restart_blacklist(self) -> List[str]:
        if not self.restart_blacklist_hosts:
            return []
        return [h.strip() for h in self.restart_blacklist_hosts.split(",") if h.strip()]

    @property
    def email_recipients_list(self) -> List[str]:
        if not self.notify_email_recipients:
            return []
        return [e.strip() for e in self.notify_email_recipients.split(",") if e.strip()]


# 全局单例
settings = Settings()


def ensure_dirs():
    """确保必要的目录存在"""
    for path_str in [settings.log_file, settings.audit_log_file]:
        Path(path_str).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.chroma_persist_dir).mkdir(parents=True, exist_ok=True)


ensure_dirs()
