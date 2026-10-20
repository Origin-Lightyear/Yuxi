"""网关运行配置，全部来自环境变量。"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(slots=True)
class GatewaySettings:
    """网关设置：Yuxi 地址、服务令牌与轮询间隔。"""

    api_base: str
    gateway_token: str
    poll_interval: float

    @classmethod
    def from_env(cls) -> GatewaySettings:
        """从环境变量构造配置。"""
        return cls(
            api_base=os.environ.get("YUXI_GATEWAY_API_BASE", "http://api:5050").rstrip("/"),
            gateway_token=os.environ.get("IM_GATEWAY_TOKEN", ""),
            poll_interval=float(os.environ.get("IM_GATEWAY_POLL_INTERVAL", "30")),
        )
