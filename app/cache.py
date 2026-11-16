import threading
import time
from collections.abc import Callable
from typing import TypeVar, cast

T = TypeVar("T")
MAX_CACHE_ENTRIES = 512


class TTLCache:
    def __init__(self, ttl_seconds: int) -> None:
        self._ttl_seconds = ttl_seconds
        self._entries: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def get_or_load(
        self,
        key: str,
        loader: Callable[[], T],
        *,
        force_refresh: bool = False,
    ) -> T:
        if self._ttl_seconds == 0:
            return loader()
        now = time.monotonic()
        if not force_refresh:
            with self._lock:
                entry = self._entries.get(key)
                if entry is not None and entry[0] > now:
                    return cast(T, entry[1])

        value = loader()
        with self._lock:
            now = time.monotonic()
            self._entries = {
                entry_key: entry_value
                for entry_key, entry_value in self._entries.items()
                if entry_value[0] > now
            }
            if len(self._entries) >= MAX_CACHE_ENTRIES:
                self._entries.pop(next(iter(self._entries)))
            self._entries[key] = (now + self._ttl_seconds, value)
        return value

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
