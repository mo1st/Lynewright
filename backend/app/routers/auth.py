"""Authentication and user-management routes.

Login exchanges email+password for an opaque session token (sent thereafter as
``Authorization: Bearer <token>``). User management is admin-only. All of this
operates on the single shared workspace — roles gate actions, not data.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status

from app.config import get_settings
from app.database import (
    USER_ROLES,
    authenticate_user,
    count_admins,
    create_session,
    create_user,
    delete_other_sessions_for_user,
    delete_user_account,
    delete_session,
    delete_sessions_for_user,
    describe_login_failure,
    get_user_by_id,
    list_users,
    set_user_active,
    set_user_email,
    set_user_password,
    update_user_role,
)
from app.models.auth import (
    CreateUserRequest,
    CurrentUser,
    ChangeMyEmailRequest,
    ChangeMyPasswordRequest,
    LoginRequest,
    LoginResponse,
    UpdateUserRequest,
    UserOut,
)
from app.security import _bearer_token, require_admin, require_user
from app.services.application_logging import log_event
from app.services.rate_limiter import RateLimiterUnavailable, get_rate_limiter
from app.services.tenancy import list_user_tenants

router = APIRouter(prefix="/api/auth", tags=["auth"])


_LOGIN_WINDOW_SECONDS = 300
# The 401 the visitor sees is deliberately uniform; the log is where an admin
# finds out which of the three causes it actually was.
_LOGIN_FAILURE_HINTS = {
    "no_such_account": "No user with this email exists. Invite them from Settings → Users.",
    "account_inactive": "The account exists but is deactivated. Re-activate it in Settings → Users.",
    "bad_password": "The account exists and is active; the password was wrong. Reset it if needed.",
}


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _enforce_login_rate_limit(request: Request, email: str) -> None:
    """Cap sign-in attempts per source address and per account.

    Both counters spend on every attempt, not just failures: the limiter has no
    peek, and an attacker interleaving a known-good password to reset a
    failure-only counter would walk straight through it.
    """
    settings = get_settings()
    limiter = get_rate_limiter()
    address = _client_ip(request)
    checks = (
        ("auth_login_ip", address, settings.login_rate_limit_per_ip),
        ("auth_login_account", email, settings.login_rate_limit_per_account),
    )
    for namespace, key, maximum in checks:
        if limiter.consume(
            namespace=namespace, key=key, maximum=maximum,
            window_seconds=_LOGIN_WINDOW_SECONDS,
        ):
            continue
        log_event(
            level="warning", category="auth", source="auth.login",
            message=f"Sign-in blocked by rate limit ({namespace}).",
            details={
                "scope": namespace, "ip": address, "email": email,
                "limit": maximum, "window_seconds": _LOGIN_WINDOW_SECONDS,
                "failure_kind": "rate_limited",
                "hint": (
                    "One source IP exceeded the attempt ceiling."
                    if namespace == "auth_login_ip"
                    else "One account exceeded the attempt ceiling; it may be under a guessing attempt."
                ),
            },
            request_path="/api/auth/login",
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many sign-in attempts. Try again in a few minutes.",
            headers={"Retry-After": str(_LOGIN_WINDOW_SECONDS)},
        )


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, request: Request) -> dict[str, Any]:
    email = payload.email.strip().lower()
    try:
        _enforce_login_rate_limit(request, email)
    except RateLimiterUnavailable as exc:
        # No limiter means no ceiling on guessing, so refuse rather than accept
        # unbounded attempts against an account that controls provider keys.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sign-in is temporarily unavailable.",
        ) from exc
    user = authenticate_user(email, payload.password)
    if user is None:
        reason = describe_login_failure(email)
        log_event(
            level="warning", category="auth", source="auth.login",
            message=f"Failed sign-in attempt for {email} ({reason}).",
            details={
                "ip": _client_ip(request),
                "email": email,
                "failure_kind": reason,
                "user_agent": request.headers.get("user-agent", "")[:200],
                "hint": _LOGIN_FAILURE_HINTS.get(reason, ""),
            },
            request_path="/api/auth/login",
        )
        detail = "This account is disabled. Ask an admin." if reason == "account_inactive" else "Invalid email or password."
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)
    token = create_session(int(user["id"]), get_settings().session_ttl_hours)
    return {"token": token, "user": user}


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def logout(authorization: str | None = Header(default=None)) -> Response:
    token = _bearer_token(authorization)
    if token:
        delete_session(token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def logout_all(
    keep_current: bool = Query(default=False),
    authorization: str | None = Header(default=None),
    user: dict[str, Any] = Depends(require_user),
) -> Response:
    """Revoke this user's sessions, optionally keeping the calling session."""
    if not bool(user.get("synthetic", False)):
        if keep_current:
            token = _bearer_token(authorization)
            if token:
                delete_other_sessions_for_user(int(user["id"]), token)
            else:
                delete_sessions_for_user(int(user["id"]))
        else:
            delete_sessions_for_user(int(user["id"]))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=CurrentUser)
def me(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    synthetic = bool(user.get("synthetic", False))
    tenants = list_user_tenants(int(user.get("id", 0)), synthetic=synthetic)
    active_tenant_id = str(user.get("active_tenant_id") or "") or (str(tenants[0]["id"]) if tenants else None)
    return {
        "id": int(user.get("id", 0)),
        "email": str(user.get("email", "")),
        "role": str(user.get("role", "viewer")),
        "is_active": bool(user.get("is_active", True)),
        "synthetic": synthetic,
        "active_tenant_id": active_tenant_id,
        "tenants": tenants,
    }


def _enforce_password_change_rate_limit(request: Request, email: str) -> None:
    settings = get_settings()
    limiter = get_rate_limiter()
    address = _client_ip(request)
    checks = (
        ("auth_password_ip", address, settings.login_rate_limit_per_ip),
        ("auth_password_account", email, settings.login_rate_limit_per_account),
    )
    for namespace, key, maximum in checks:
        if limiter.consume(
            namespace=namespace, key=key, maximum=maximum,
            window_seconds=_LOGIN_WINDOW_SECONDS,
        ):
            continue
        log_event(
            level="warning", category="auth", source="auth.password",
            message="Password change blocked by rate limit.",
            details={"user_id": None, "scope": namespace, "ip": address, "email": email,
                     "limit": maximum, "window_seconds": _LOGIN_WINDOW_SECONDS},
            request_path="/api/auth/me/password",
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many password changes. Try again in a few minutes.",
            headers={"Retry-After": str(_LOGIN_WINDOW_SECONDS)},
        )


@router.post("/me/password")
def change_my_password(
    payload: ChangeMyPasswordRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, bool]:
    if bool(user.get("synthetic", False)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password change is unavailable for this account.")
    try:
        _enforce_password_change_rate_limit(request, str(user.get("email", "")))
    except RateLimiterUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Password change is temporarily unavailable.") from exc
    if authenticate_user(str(user.get("email", "")), payload.current_password) is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect.")
    token = _bearer_token(authorization)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    user_id = int(user["id"])
    set_user_password(user_id, payload.new_password, keep_session_token=token)
    log_event(
        level="info", category="auth", source="auth.password",
        message="Account password changed; other sessions were revoked.",
        details={"user_id": user_id, "other_sessions_revoked": True},
        request_path="/api/auth/me/password",
    )
    return {"changed": True}


@router.post("/me/email")
def change_my_email(
    payload: ChangeMyEmailRequest,
    authorization: str | None = Header(default=None),
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, str]:
    if bool(user.get("synthetic", False)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email change is unavailable for this account.")
    if authenticate_user(str(user.get("email", "")), payload.current_password) is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect.")
    token = _bearer_token(authorization)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    try:
        updated = set_user_email(int(user["id"]), payload.new_email, keep_session_token=token)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    log_event(
        level="info", category="auth", source="auth.email",
        message="Account email changed; other sessions were revoked.",
        details={"user_id": int(user["id"]), "other_sessions_revoked": True},
        request_path="/api/auth/me/email",
    )
    return {"email": str(updated["email"])}


@router.get("/users", response_model=list[UserOut], dependencies=[Depends(require_admin)])
def read_users() -> list[dict[str, Any]]:
    return list_users()


@router.post(
    "/users",
    response_model=UserOut,
    dependencies=[Depends(require_admin)],
    status_code=status.HTTP_201_CREATED,
)
def add_user(payload: CreateUserRequest) -> dict[str, Any]:
    if payload.role not in USER_ROLES:
        raise HTTPException(status_code=422, detail=f"Unknown role: {payload.role}")
    try:
        return create_user(payload.email, payload.password, payload.role)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.patch("/users/{user_id}", response_model=UserOut, dependencies=[Depends(require_admin)])
def edit_user(user_id: int, payload: UpdateUserRequest) -> dict[str, Any]:
    target = get_user_by_id(user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    # Guard against locking out the last active admin.
    demoting = payload.role is not None and payload.role != "admin" and target["role"] == "admin"
    deactivating = payload.is_active is False and target["role"] == "admin" and target["is_active"]
    if (demoting or deactivating) and count_admins() <= 1:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cannot remove the last active admin.")

    if payload.role is not None:
        if payload.role not in USER_ROLES:
            raise HTTPException(status_code=422, detail=f"Unknown role: {payload.role}")
        update_user_role(user_id, payload.role)
    if payload.is_active is not None:
        set_user_active(user_id, payload.is_active)
    if payload.password is not None:
        set_user_password(user_id, payload.password)
    if payload.email is not None:
        try:
            updated = set_user_email(user_id, payload.email)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        if updated is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    return get_user_by_id(user_id)


@router.delete("/users/{user_id}", dependencies=[Depends(require_admin)])
def remove_user(user_id: int, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    try:
        deleted = delete_user_account(user_id, actor_id=int(user["id"]))
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    log_event(
        level="info", category="auth", source="auth.users",
        message="User account deleted.",
        details={"user_id": user_id, "deleted_by": int(user["id"]), "email": deleted["email"]},
        request_path=f"/api/auth/users/{user_id}",
    )
    return deleted
