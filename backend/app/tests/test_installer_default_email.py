"""Keep the installer's bootstrap address accepted by the login request model."""
from __future__ import annotations

import re
from pathlib import Path

from app.models.auth import LoginRequest


def test_installer_default_email_is_accepted_by_login_validator() -> None:
    installer = Path(__file__).resolve().parents[3] / "install.sh"
    match = re.search(
        r"admin_email=\$\{LYNEWRIGHT_ADMIN_EMAIL:-([^}]+)\}",
        installer.read_text(encoding="utf-8"),
    )

    assert match is not None, "installer must declare a default bootstrap email"
    request = LoginRequest(email=match.group(1), password="test-password")
    assert request.email == "admin@example.com"
