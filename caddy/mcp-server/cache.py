"""进程内 TTL + LRU 缓存：识图结果、搜图结果复用，减少重复调用上游与额度消耗。"""
from __future__ import annotations

import time
import hashlib
from collections import OrderedDict
from typing import Any, Optional


class TTLCache:
    def __init__(self, maxsize: int = 256) -> None:
        self.maxsize = maxsize
        self._data: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()

    def get(self, key: str) -> Optional[Any]:
        item = self._data.get(key)
        if not item:
            return None
        expires, value = item
        if expires < time.monotonic():
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        if ttl <= 0:
            return
        self._data[key] = (time.monotonic() + ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()


def make_key(*parts: Any) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


vision_cache = TTLCache(maxsize=256)
search_cache = TTLCache(maxsize=256)
