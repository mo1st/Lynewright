"""Resolve the real client address behind the bundled dashboard proxy.

Every browser request reaches the API through the dashboard's nginx, so the
socket peer is always that container. Rate limits keyed on the peer would then
be shared by every visitor. nginx overwrites ``X-Real-IP`` with the address it
accepted the connection from, so the header is trusted only when the peer is a
configured proxy; any other caller (an integration container, a direct client)
keeps its own socket address and cannot spoof one.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import threading
import time

from starlette.requests import Request

_CACHE_SECONDS = 30.0
_lock = threading.Lock()
_cached: tuple[float, str, frozenset[str]] = (0.0, "", frozenset())


def _resolve(entry: str) -> set[str]:
    try:
        return {str(ipaddress.ip_address(entry))}
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(entry, None, type=socket.SOCK_STREAM)
    except OSError:
        return set()
    return {str(info[4][0]) for info in infos if info[4]}


def trusted_proxy_addresses() -> frozenset[str]:
    global _cached
    configured = os.getenv("LYNEWRIGHT_TRUSTED_PROXY_HOSTS", "").strip()
    now = time.monotonic()
    with _lock:
        stamp, source, addresses = _cached
        if source == configured and now - stamp < _CACHE_SECONDS:
            return addresses
    resolved: set[str] = set()
    for entry in (item.strip() for item in configured.split(",")):
        if entry:
            resolved |= _resolve(entry)
    result = frozenset(resolved)
    with _lock:
        _cached = (now, configured, result)
    return result


def client_address(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    if peer in trusted_proxy_addresses():
        forwarded = request.headers.get("x-real-ip", "").strip()
        try:
            return str(ipaddress.ip_address(forwarded))
        except ValueError:
            pass
    return peer
