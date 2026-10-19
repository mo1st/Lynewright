from __future__ import annotations

from starlette.requests import Request

from app.services import client_address as module


def _request(peer: str, headers: dict[str, str]) -> Request:
    return Request({
        "type": "http", "method": "GET", "path": "/", "query_string": b"",
        "client": (peer, 5000),
        "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
    })


def _trust(monkeypatch, value: str) -> None:
    monkeypatch.setenv("LYNEWRIGHT_TRUSTED_PROXY_HOSTS", value)
    monkeypatch.setattr(module, "_cached", (0.0, "", frozenset()))


def test_trusted_proxy_forwards_the_real_client(monkeypatch) -> None:
    _trust(monkeypatch, "10.89.0.5")
    request = _request("10.89.0.5", {"X-Real-IP": "203.0.113.9"})
    assert module.client_address(request) == "203.0.113.9"


def test_untrusted_peer_cannot_spoof_its_address(monkeypatch) -> None:
    _trust(monkeypatch, "10.89.0.5")
    request = _request("10.89.0.77", {"X-Real-IP": "203.0.113.9", "X-Forwarded-For": "198.51.100.1"})
    assert module.client_address(request) == "10.89.0.77"


def test_invalid_forwarded_value_falls_back_to_peer(monkeypatch) -> None:
    _trust(monkeypatch, "10.89.0.5")
    request = _request("10.89.0.5", {"X-Real-IP": "not-an-ip"})
    assert module.client_address(request) == "10.89.0.5"


def test_no_trusted_proxies_uses_peer(monkeypatch) -> None:
    _trust(monkeypatch, "")
    request = _request("10.89.0.5", {"X-Real-IP": "203.0.113.9"})
    assert module.client_address(request) == "10.89.0.5"


def test_proxy_hostnames_are_resolved(monkeypatch) -> None:
    _trust(monkeypatch, "dashboard")
    monkeypatch.setattr(module.socket, "getaddrinfo", lambda *_args, **_kwargs: [(2, 1, 6, "", ("10.89.0.5", 0))])
    assert module.client_address(_request("10.89.0.5", {"X-Real-IP": "203.0.113.9"})) == "203.0.113.9"
