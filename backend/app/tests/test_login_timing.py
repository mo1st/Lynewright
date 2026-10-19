from __future__ import annotations

from app import database


def test_unknown_email_still_runs_a_password_check(monkeypatch) -> None:
    checked: list[str] = []
    monkeypatch.setattr(database, "verify_password", lambda password, stored: checked.append(stored) or False)
    assert database.authenticate_user("nobody@example.com", "any-password-value") is None
    assert len(checked) == 1 and checked[0].startswith("scrypt$")
