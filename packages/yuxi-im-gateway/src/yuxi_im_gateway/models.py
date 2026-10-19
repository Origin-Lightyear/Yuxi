"""网关内部使用的归一化数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class BotConfig:
    """一个 IM 机器人配置（网关侧视图）。"""

    id: int
    platform: str
    name: str
    credentials: dict
    api_key: str
    agent_slug: str
    allowed_users: list[str] = field(default_factory=list)
    require_mention: bool = True
    group_sessions_per_user: bool = True

    @classmethod
    def from_payload(cls, payload: dict) -> BotConfig:
        """从后台配置接口返回项构造。"""
        return cls(
            id=int(payload["id"]),
            platform=str(payload["platform"]),
            name=str(payload.get("name") or ""),
            credentials=dict(payload.get("credentials") or {}),
            api_key=str(payload.get("api_key") or ""),
            agent_slug=str(payload.get("agent_slug") or ""),
            allowed_users=[str(item) for item in (payload.get("allowed_users") or [])],
            require_mention=bool(payload.get("require_mention", True)),
            group_sessions_per_user=bool(payload.get("group_sessions_per_user", True)),
        )


@dataclass(slots=True)
class InboundMessage:
    """平台事件归一化后的入站消息。"""

    message_id: str
    chat_id: str
    sender_id: str
    is_group: bool
    mentions_bot: bool
    text: str
