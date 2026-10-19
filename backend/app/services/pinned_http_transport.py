"""HTTPX transport that pins TCP to an already validated public address set."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import httpcore
import httpx
from httpcore._backends.auto import AutoBackend
from httpcore._backends.base import (
    AsyncNetworkBackend, AsyncNetworkStream, NetworkBackend, NetworkStream, SOCKET_OPTION,
)
from httpcore._backends.sync import SyncBackend

from app.services.url_security import ResolvedPublicTarget


class PinnedNetworkBackend(NetworkBackend):
    """Substitute only the TCP destination; HTTP Core retains hostname for TLS."""

    def __init__(
        self,
        target: ResolvedPublicTarget,
        *,
        backend: NetworkBackend | None = None,
    ) -> None:
        self.target = target
        self.backend = backend or SyncBackend()
        self.connection_attempts: list[str] = []

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> NetworkStream:
        normalized_host = host.decode("ascii") if isinstance(host, bytes) else str(host)
        if normalized_host.rstrip(".").lower() != self.target.hostname or port != self.target.port:
            raise httpcore.ConnectError("Pinned transport target does not match the validated origin.")
        last_error: Exception | None = None
        for address in self.target.addresses:
            self.connection_attempts.append(address)
            try:
                return self.backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:  # HTTP Core maps the final network error for HTTPX.
                last_error = exc
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectError("Validated target has no connectable addresses.")

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> NetworkStream:
        raise httpcore.ConnectError("Unix sockets are not allowed for outbound HTTP.")

    def sleep(self, seconds: float) -> None:
        self.backend.sleep(seconds)


class PinnedHTTPTransport(httpx.HTTPTransport):
    """Standard verified HTTP transport with a validated-IP TCP backend."""

    def __init__(self, target: ResolvedPublicTarget) -> None:
        # No proxy is configured, and trust_env=False prevents environment CA
        # or proxy settings from changing this outbound security boundary.
        super().__init__(verify=True, trust_env=False, retries=0)
        self.network_backend = PinnedNetworkBackend(target)
        self._pool._network_backend = self.network_backend


class AsyncPinnedNetworkBackend(AsyncNetworkBackend):
    """Async twin of PinnedNetworkBackend for async HTTP clients."""

    def __init__(self, target: ResolvedPublicTarget, *, backend: AsyncNetworkBackend | None = None) -> None:
        self.target = target
        self.backend = backend or AutoBackend()
        self.connection_attempts: list[str] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> AsyncNetworkStream:
        normalized_host = host.decode("ascii") if isinstance(host, bytes) else str(host)
        if normalized_host.rstrip(".").lower() != self.target.hostname or port != self.target.port:
            raise httpcore.ConnectError("Pinned transport target does not match the validated origin.")
        last_error: Exception | None = None
        for address in self.target.addresses:
            self.connection_attempts.append(address)
            try:
                return await self.backend.connect_tcp(
                    address, port, timeout=timeout,
                    local_address=local_address, socket_options=socket_options,
                )
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectError("Validated target has no connectable addresses.")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> AsyncNetworkStream:
        raise httpcore.ConnectError("Unix sockets are not allowed for outbound HTTP.")

    async def sleep(self, seconds: float) -> None:
        await self.backend.sleep(seconds)


class AsyncPinnedHTTPTransport(httpx.AsyncHTTPTransport):
    """Async verified HTTP transport with a validated-IP TCP backend."""

    def __init__(self, target: ResolvedPublicTarget) -> None:
        super().__init__(verify=True, trust_env=False, retries=0)
        self.network_backend = AsyncPinnedNetworkBackend(target)
        self._pool._network_backend = self.network_backend


def peer_address(response: httpx.Response) -> Any:
    """Diagnostic helper; absent for mocked and non-socket responses."""
    stream = response.extensions.get("network_stream")
    return stream.get_extra_info("server_addr") if stream is not None else None
