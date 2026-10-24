"""带 TTL 与容量上限的消息去重缓存。"""

from __future__ import annotations

import time
from collections import OrderedDict


class DedupeCache:
    """按 key 记录最近处理时间，避免重复处理同一条平台消息。"""

    def __init__(self, max_size: int = 4096, ttl_seconds: float = 3600) -> None:
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._entries: OrderedDict[str, float] = OrderedDict()

    def seen(self, key: str, now: float | None = None) -> bool:
        """返回该 key 是否在 TTL 内已见过；未见则记录并返回 False。"""
        current = time.monotonic() if now is None else now
        self._evict_expired(current)
        last = self._entries.get(key)
        if last is not None and current - last < self._ttl:
            return True
        self._entries[key] = current
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_size:
            self._entries.popitem(last=False)
        return False

    def _evict_expired(self, now: float) -> None:
        """清理已过期条目。"""
        expired = [key for key, ts in self._entries.items() if now - ts >= self._ttl]
        for key in expired:
            self._entries.pop(key, None)
