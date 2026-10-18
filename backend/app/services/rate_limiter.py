from __future__ import annotations

import hashlib
import threading
import time
from collections import deque
from typing import Any, Protocol

from app.config import get_settings


class RateLimiter(Protocol):
    def consume(self, *, namespace: str, key: str, maximum: int, window_seconds: int) -> bool: ...
    def consume_many(self, *, namespace: str, key: str, amount: int, maximum: int, window_seconds: int) -> bool: ...


class RateLimiterUnavailable(RuntimeError):
    pass


class MemoryRateLimiter:
    def __init__(self, *, max_keys: int = 10_000, cleanup_interval_seconds: float = 60) -> None:
        self.max_keys = max(1, max_keys)
        self.cleanup_interval_seconds = max(1.0, cleanup_interval_seconds)
        self._windows: dict[str, deque[tuple[float, int]]] = {}
        self._window_seconds: dict[str, int] = {}
        self._lock = threading.Lock()
        self._last_cleanup = 0.0

    @property
    def key_count(self) -> int: return len(self._windows)

    def consume(self, *, namespace: str, key: str, maximum: int, window_seconds: int) -> bool:
        return self.consume_many(namespace=namespace, key=key, amount=1, maximum=maximum, window_seconds=window_seconds)

    def consume_many(self, *, namespace: str, key: str, amount: int, maximum: int, window_seconds: int) -> bool:
        if amount < 1:
            return False
        now = time.monotonic(); window_seconds = max(1, window_seconds); maximum = max(1, maximum)
        if amount > maximum:
            return False
        storage_key = _storage_key(namespace, key)
        with self._lock:
            if now - self._last_cleanup >= self.cleanup_interval_seconds:
                for existing, window in list(self._windows.items()):
                    existing_window = self._window_seconds.get(existing, window_seconds)
                    if not window or now - window[-1][0] >= existing_window:
                        self._windows.pop(existing, None)
                        self._window_seconds.pop(existing, None)
                self._last_cleanup = now
            window = self._windows.get(storage_key)
            if window is None:
                if len(self._windows) >= self.max_keys: return False
                window = deque(); self._windows[storage_key] = window
                self._window_seconds[storage_key] = window_seconds
            while window and now - window[0][0] >= window_seconds: window.popleft()
            if not window:
                self._windows.pop(storage_key, None)
                self._window_seconds.pop(storage_key, None)
                if len(self._windows) >= self.max_keys: return False
                window = deque(); self._windows[storage_key] = window
                self._window_seconds[storage_key] = window_seconds
            if sum(entry_amount for _, entry_amount in window) + amount > maximum:
                return False
            window.append((now, amount)); return True


class RedisRateLimiter:
    _SCRIPT = """
local amount = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local maximum = tonumber(ARGV[3])
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if amount < 1 or amount > maximum or current + amount > maximum then return 0 end
local updated = redis.call('INCRBY', KEYS[1], amount)
if current == 0 then redis.call('EXPIRE', KEYS[1], window) end
return updated > 0 and 1 or 0
"""

    def __init__(self, client: Any, *, key_prefix: str = "lynewright:rate") -> None:
        self.client = client; self.key_prefix = key_prefix.rstrip(":")

    def consume(self, *, namespace: str, key: str, maximum: int, window_seconds: int) -> bool:
        return self.consume_many(namespace=namespace, key=key, amount=1, maximum=maximum, window_seconds=window_seconds)

    def consume_many(self, *, namespace: str, key: str, amount: int, maximum: int, window_seconds: int) -> bool:
        if amount < 1 or amount > max(1, maximum):
            return False
        storage_key = f"{self.key_prefix}:{_storage_key(namespace, key)}"
        try:
            result = self.client.eval(self._SCRIPT, 1, storage_key, amount, max(1, window_seconds), max(1, maximum))
        except Exception as exc:  # Redis outages must fail closed for public endpoints.
            raise RateLimiterUnavailable("Distributed rate limiter is unavailable.") from exc
        return int(result) == 1


_instance: RateLimiter | None = None
_instance_signature = ""
_instance_lock = threading.Lock()


def get_rate_limiter() -> RateLimiter:
    global _instance, _instance_signature
    settings = get_settings(); signature = f"{settings.redis_url}|{settings.worker_count}|{settings.rate_limit_max_keys}"
    with _instance_lock:
        if _instance is not None and _instance_signature == signature: return _instance
        if settings.redis_url:
            try:
                import redis
                client = redis.Redis.from_url(
                    settings.redis_url, decode_responses=True,
                    socket_connect_timeout=1.0, socket_timeout=1.0,
                    max_connections=20, health_check_interval=30,
                )
            except Exception as exc:
                raise RateLimiterUnavailable("Redis rate limiter could not be configured.") from exc
            _instance = RedisRateLimiter(client)
        elif settings.worker_count > 1:
            raise RateLimiterUnavailable("LYNEWRIGHT_REDIS_URL is required when multiple workers are configured.")
        else:
            _instance = MemoryRateLimiter(max_keys=settings.rate_limit_max_keys)
        _instance_signature = signature
        return _instance


def rate_limiter_mode() -> str:
    settings = get_settings()
    if settings.redis_url: return "redis"
    if settings.worker_count > 1: return "unavailable"
    return "memory"


def _storage_key(namespace: str, key: str) -> str:
    safe_namespace = "".join(character for character in namespace.lower() if character.isalnum() or character in "_-")[:50] or "default"
    return f"{safe_namespace}:{hashlib.sha256(key.encode('utf-8')).hexdigest()}"
