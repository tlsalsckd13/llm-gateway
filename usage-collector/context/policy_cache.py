from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Generic, TypeVar


T = TypeVar("T")


@dataclass
class TTLValue(Generic[T]):
    value: T
    expires_at: float


class TTLCache(Generic[T]):
    def __init__(self, ttl_seconds: int = 60):
        self.ttl_seconds = ttl_seconds
        self._values: dict[str, TTLValue[T]] = {}

    def get(self, key: str) -> T | None:
        item = self._values.get(key)
        if not item:
            return None
        if item.expires_at < time.time():
            self._values.pop(key, None)
            return None
        return item.value

    def set(self, key: str, value: T) -> None:
        self._values[key] = TTLValue(value=value, expires_at=time.time() + self.ttl_seconds)

    def clear(self) -> None:
        self._values.clear()
