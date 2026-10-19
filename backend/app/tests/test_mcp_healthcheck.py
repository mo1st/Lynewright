from __future__ import annotations

from urllib.error import HTTPError

from app import mcp_healthcheck


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_: object) -> None:
        return None


def test_mcp_healthcheck_accepts_the_expected_unauthenticated_response(monkeypatch) -> None:
    monkeypatch.setattr(mcp_healthcheck, "urlopen", lambda *_args, **_kwargs: _Response(401))
    assert mcp_healthcheck.main() == 0


def test_mcp_healthcheck_accepts_unauthorized_http_error(monkeypatch) -> None:
    def denied(*_args: object, **_kwargs: object) -> _Response:
        raise HTTPError(mcp_healthcheck.HEALTH_URL, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(mcp_healthcheck, "urlopen", denied)
    assert mcp_healthcheck.main() == 0


def test_mcp_healthcheck_rejects_an_unexpected_response(monkeypatch) -> None:
    monkeypatch.setattr(mcp_healthcheck, "urlopen", lambda *_args, **_kwargs: _Response(500))
    assert mcp_healthcheck.main() == 1
