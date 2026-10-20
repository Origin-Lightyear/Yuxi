"""平台适配器抽象：网关只依赖该接口，不依赖具体平台 SDK。"""

from __future__ import annotations

from abc import ABC, abstractmethod


class Adapter(ABC):
    """一个机器人对应的平台连接与出站能力。"""

    @abstractmethod
    async def start(self) -> None:
        """建立长连接并开始接收事件。"""

    @abstractmethod
    async def stop(self) -> None:
        """关闭连接，释放资源。"""

    @abstractmethod
    async def send_text(self, chat_id: str, text: str) -> None:
        """发送纯文本消息。"""

    @abstractmethod
    async def send_markdown(self, chat_id: str, text: str) -> None:
        """发送 markdown 富文本消息，失败时降级为纯文本。"""

    @abstractmethod
    async def send_file(self, chat_id: str, filename: str, content: bytes) -> None:
        """发送文件附件。"""

    @abstractmethod
    async def send_approval_card(self, chat_id: str, *, thread_id: str, run_id: str, requester_id: str) -> None:
        """发送工具审批交互卡片。"""
