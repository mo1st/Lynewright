from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.auth import CreateUserRequest
from app.passwords import MAX_PASSWORD_LENGTH, password_policy_error
from app.services import auth_bootstrap


def test_policy_rejects_short_long_and_published_passwords() -> None:
    assert password_policy_error("eleven-char") != ""
    assert password_policy_error("x" * (MAX_PASSWORD_LENGTH + 1)) != ""
    assert password_policy_error("change-this-placeholder-before-first-login") != ""
    assert password_policy_error("a-private-passphrase") == ""


def test_api_models_require_twelve_characters() -> None:
    with pytest.raises(ValidationError):
        CreateUserRequest(email="person@example.com", password="elevenchars", role="viewer")
    CreateUserRequest(email="person@example.com", password="twelve-chars", role="viewer")


@pytest.mark.parametrize("password", ["short", "change-this-placeholder-before-first-login"])
def test_bootstrap_refuses_weak_passwords(monkeypatch, password: str) -> None:
    created: list[str] = []
    monkeypatch.setenv("LYNEWRIGHT_BOOTSTRAP_EMAIL", "admin@example.com")
    monkeypatch.setenv("LYNEWRIGHT_BOOTSTRAP_PASSWORD", password)
    monkeypatch.setattr(auth_bootstrap, "get_user_by_email", lambda _email: None)
    monkeypatch.setattr(auth_bootstrap, "create_user", lambda email, *_args, **_kwargs: created.append(email))
    monkeypatch.setattr(auth_bootstrap, "log_event", lambda **_kwargs: None)

    assert auth_bootstrap.bootstrap_admin_user() is False
    assert created == []


def test_bootstrap_accepts_generated_password(monkeypatch) -> None:
    created: list[str] = []
    monkeypatch.setenv("LYNEWRIGHT_BOOTSTRAP_EMAIL", "admin@example.com")
    monkeypatch.setenv("LYNEWRIGHT_BOOTSTRAP_PASSWORD", "a-long-private-passphrase")
    monkeypatch.setattr(auth_bootstrap, "get_user_by_email", lambda _email: None)
    monkeypatch.setattr(auth_bootstrap, "create_user", lambda email, *_args, **_kwargs: created.append(email))
    monkeypatch.setattr(auth_bootstrap, "log_event", lambda **_kwargs: None)

    assert auth_bootstrap.bootstrap_admin_user() is True
    assert created == ["admin@example.com"]
