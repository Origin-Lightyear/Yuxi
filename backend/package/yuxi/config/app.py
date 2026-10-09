"""应用配置模块。

只保留仍被消费的应用级配置（SaaS 开关、租户密钥、默认 Embedding 模型）。
管理员可改的运行时配置已迁移到 `yuxi.config.options`（PostgreSQL ConfigOption），
Redis 运行时配置同步见 `yuxi.config` 历史重构 #915。
"""

from __future__ import annotations

import os

from pydantic import BaseModel, Field

DEFAULT_EMBED_MODEL = "siliconflow-cn:Pro/BAAI/bge-m3"
DEFAULT_EMBED_FALLBACK_MODEL = "ollama:bge-m3"


class Config(BaseModel):
    """应用配置类。

    `is_saas_enabled` 与 `is_agent_data_sync_enabled` 是唯一的开关判定点，
    调用方不得自行解释 `SAAS_ENABLED`/`AGENT_DATA_SYNC` 环境变量。
    """

    saas_enabled: bool = Field(default=False, description="是否启用 SaaS 多租户模式")
    platform_service_url: str = Field(default="", description="Platform 内部服务地址")
    agent_data_sync_enabled: bool | None = Field(
        default=None,
        description="SaaS 模式下是否双写同步 Agent 数据到 Tenant 服务（默认跟随 SaaS 开关）",
    )
    tenant_admin_api_key: str = Field(
        default="",
        description="Tenant 服务调用租户知识库管理 API 的静态密钥（Tenant 侧持有）",
    )
    embed_model: str = Field(default=DEFAULT_EMBED_MODEL, description="默认 Embedding 模型")
    embed_fallback_model: str = Field(
        default=DEFAULT_EMBED_FALLBACK_MODEL,
        description="Embedding 模型未注册时的降级模型",
    )

    model_config = {"extra": "allow"}

    def __init__(self, **data):
        super().__init__(**data)
        self._apply_env_defaults()

    def _apply_env_defaults(self) -> None:
        """从环境变量读取配置作为初始值。"""
        env_saas = os.getenv("SAAS_ENABLED", "").lower()
        if env_saas in ("true", "1"):
            self.saas_enabled = True
        elif env_saas in ("false", "0"):
            self.saas_enabled = False

        env_agent_data_sync = os.getenv("AGENT_DATA_SYNC", "").strip().lower()
        if env_agent_data_sync in ("true", "1"):
            self.agent_data_sync_enabled = True
        elif env_agent_data_sync in ("false", "0"):
            self.agent_data_sync_enabled = False

        for field_name, env_key in (
            ("platform_service_url", "PLATFORM_SERVICE_URL"),
            ("tenant_admin_api_key", "TENANT_ADMIN_API_KEY"),
        ):
            env_val = os.getenv(env_key, "").strip()
            if env_val:
                setattr(self, field_name, env_val)

    @property
    def is_saas_enabled(self) -> bool:
        """SaaS 模式是否可用：开关打开且服务地址已配置。"""
        return self.saas_enabled and bool(self.platform_service_url)

    @property
    def is_agent_data_sync_enabled(self) -> bool:
        """Agent 数据双写是否启用：SaaS 可用且未被显式关闭。"""
        if not self.is_saas_enabled:
            return False
        if self.agent_data_sync_enabled is None:
            return True
        return self.agent_data_sync_enabled


config = Config()
