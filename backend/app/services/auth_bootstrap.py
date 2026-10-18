"""First-run admin bootstrap.

When LYNEWRIGHT_BOOTSTRAP_EMAIL and LYNEWRIGHT_BOOTSTRAP_PASSWORD are set and that user
does not exist yet, provision an admin so a fresh deployment has a way in without
the break-glass token. Idempotent: skips when the user already exists. No open
signup — every other account is created by an admin.
"""

from __future__ import annotations

from app.config import get_settings
from app.database import count_admins, create_user, get_user_by_email
from app.services.application_logging import log_event


def bootstrap_admin_user() -> bool:
    settings = get_settings()
    email = settings.bootstrap_admin_email
    password = settings.bootstrap_admin_password
    if not email or not password:
        return False
    if get_user_by_email(email) is not None:
        return False
    try:
        create_user(email, password, role="admin")
    except ValueError as exc:  # e.g. a race created it first
        log_event(
            level="warning", category="auth", source="auth_bootstrap",
            message=f"Admin bootstrap skipped: {exc}",
            details={
                "email": email,
                "hint": "No admin exists and bootstrap failed; set LYNEWRIGHT_BOOTSTRAP_EMAIL/_PASSWORD and restart.",
            },
            exc=exc,
        )
        return False
    log_event(
        level="info", category="auth", source="auth_bootstrap",
        message="Bootstrapped initial admin user.", details={"email": email},
    )
    return True


def validate_production_admin_access() -> None:
    """Refuse a production start that has no usable administrative entry point."""
    settings = get_settings()
    if not settings.is_production:
        return
    if settings.admin_token or count_admins() > 0:
        return
    raise RuntimeError(
        "Production requires an active administrator, LYNEWRIGHT_ADMIN_TOKEN, "
        "or valid LYNEWRIGHT_BOOTSTRAP_EMAIL/LYNEWRIGHT_BOOTSTRAP_PASSWORD settings."
    )
