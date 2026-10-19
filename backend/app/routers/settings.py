from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Path

from app.database import (
    delete_ai_provider_profile, get_ai_provider_profile, get_ai_settings,
    save_ai_provider_profile, save_ai_settings,
)
from app.security import require_admin_token
from app.models.settings import (
    AI_PROVIDERS,
    AiModelsResponse,
    AiProviderTestResponse,
    AiSettingsPayload,
    AiSettingsRead,
    AiProviderProfilePayload,
    AiProviderProfileRead,
    mask_key,
    sanitize_secret_input,
    CredentialDeletePayload,
    CredentialDeleteResponse,
    CredentialMetadata,
    BackupSettingsPayload,
    BackupSettingsRead,
)
from app.services.ai_provider import (
    AiProviderError,
    fetch_provider_models,
    selected_provider,
    settings_response,
    test_provider,
)
from app.services.platform_services import (
    CredentialService,
    PlatformConflictError,
    PlatformNotFoundError,
    PlatformServiceError,
)
from app.services.platform_models import CredentialSetInput
from app.services.backup_service import (
    get_backup_settings, get_backup_status, list_local_backups, run_backup_now,
    save_backup_settings, test_backup_destination, verify_remote_backup,
)


router = APIRouter(
    prefix="/api/settings",
    tags=["settings"],
    dependencies=[Depends(require_admin_token)],
)


def _credential_error(exc: PlatformServiceError) -> HTTPException:
    if isinstance(exc, PlatformNotFoundError):
        status_code = 404
    elif isinstance(exc, PlatformConflictError):
        status_code = 409
    else:
        status_code = 400
    return HTTPException(status_code=status_code, detail=exc.message)


@router.get("/credentials", response_model=list[CredentialMetadata])
def list_credentials() -> list[dict]:
    """List credential metadata only; raw secrets are never returned."""
    try:
        return CredentialService().list_metadata()
    except PlatformServiceError as exc:
        raise _credential_error(exc) from exc


@router.put("/credentials", response_model=CredentialMetadata)
def save_credential(payload: CredentialSetInput) -> dict:
    """Create or rotate a credential through the existing encrypted service."""
    try:
        return CredentialService().set(payload.model_dump())
    except PlatformServiceError as exc:
        raise _credential_error(exc) from exc


@router.get("/credentials/{name}", response_model=CredentialMetadata)
def credential_status(
    name: str = Path(..., pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$"),
) -> dict:
    """Read one credential's metadata/status without revealing its secret."""
    try:
        return CredentialService().get_status(name)
    except PlatformServiceError as exc:
        raise _credential_error(exc) from exc


@router.get("/credentials/{name}/references")
def credential_references(
    name: str = Path(..., pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$"),
) -> list[dict[str, str]]:
    """Return non-secret consumers so operators can detach credentials safely."""
    return CredentialService().references(name)


@router.delete("/credentials/{name}", response_model=CredentialDeleteResponse)
def delete_credential(
    name: str = Path(..., pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$"),
    payload: CredentialDeletePayload = Body(...),
) -> dict:
    """Delete a credential only when the operator explicitly confirms its name."""
    try:
        return CredentialService().delete(name, confirm_name=payload.confirm_name)
    except PlatformServiceError as exc:
        raise _credential_error(exc) from exc


@router.get("/backups", response_model=BackupSettingsRead)
def read_backup_configuration() -> dict:
    return {**get_backup_settings(), **get_backup_status(), "local_backups": list_local_backups()}


@router.put("/backups", response_model=BackupSettingsRead)
def write_backup_configuration(payload: BackupSettingsPayload) -> dict:
    saved = save_backup_settings(payload.model_dump())
    return {**saved, **get_backup_status(), "local_backups": list_local_backups()}


@router.post("/backups/test")
def test_backup_configuration() -> dict:
    try:
        return test_backup_destination()
    except PlatformServiceError as exc:
        raise _credential_error(exc) from exc


@router.post("/backups/run")
def run_backup_configuration() -> dict:
    return run_backup_now()


@router.post("/backups/verify-remote")
def verify_remote_backup_configuration() -> dict:
    """Independently read back the last uploaded object and compare integrity.

    Admin-only like the rest of this router. Returns status, object metadata,
    checksum match, duration, and a redacted error -- never credentials or a
    signed authorization header.
    """
    try:
        return verify_remote_backup()
    except PlatformServiceError as exc:
        raise _credential_error(exc) from exc


@router.get("/ai", response_model=AiSettingsRead)
def read_ai_provider_settings() -> dict:
    return settings_response(get_ai_settings())


@router.post("/ai", response_model=AiSettingsRead)
def save_ai_provider_settings(payload: AiSettingsPayload) -> dict:
    settings = save_ai_settings(payload.model_dump())
    return settings_response(settings)


def _profile_response(profile: dict) -> dict:
    key = str(profile.get("api_key") or "")
    credential_ref = str(profile.get("credential_ref") or "")
    credential_set = bool(credential_ref and CredentialService().get_status(credential_ref).get("configured"))
    return {
        "id": int(profile["id"]), "name": str(profile["name"]),
        "provider": str(profile["provider"]), "protocol": str(profile["protocol"]),
        "model": str(profile["model"]), "base_url": str(profile.get("base_url") or ""),
        "priority": int(profile["priority"]), "enabled": bool(profile["enabled"]),
        "api_key_set": bool(key) or credential_set, "api_key_masked": mask_key(key) if key else ("configured" if credential_set else ""),
        "credential_ref": credential_ref,
        "reasoning_effort": str(profile.get("reasoning_effort") or ""),
        "updated_at": str(profile.get("updated_at") or ""),
    }


def _profile_settings(profile: dict) -> dict:
    provider = str(profile["provider"])
    from app.database import _ai_profile_as_settings
    resolved = _ai_profile_as_settings(profile)
    key = str((resolved.get("api_keys") or {}).get(provider) or "")
    return {
        "profile_id": int(profile["id"]), "provider": provider,
        "protocol": str(profile["protocol"]), "model": str(profile["model"]),
        "base_url": str(profile.get("base_url") or ""),
        "api_keys": {provider: key} if key else {},
    }


@router.post("/ai/providers", response_model=AiProviderProfileRead)
def create_ai_provider_profile(payload: AiProviderProfilePayload) -> dict:
    return _profile_response(save_ai_provider_profile(payload.model_dump()))


@router.put("/ai/providers/{profile_id}", response_model=AiProviderProfileRead)
def update_ai_provider_profile(profile_id: int, payload: AiProviderProfilePayload) -> dict:
    try:
        profile = save_ai_provider_profile(payload.model_dump(), profile_id=profile_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _profile_response(profile)


@router.delete("/ai/providers/{profile_id}")
def remove_ai_provider_profile(profile_id: int) -> dict:
    if not delete_ai_provider_profile(profile_id):
        raise HTTPException(status_code=404, detail="AI provider profile not found.")
    return {"deleted": True}


@router.post("/ai/providers/{profile_id}/test", response_model=AiProviderTestResponse)
def test_saved_ai_provider_profile(profile_id: int) -> dict:
    profile = get_ai_provider_profile(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="AI provider profile not found.")
    return test_provider(_profile_settings(profile))


@router.post("/ai/providers/{profile_id}/models", response_model=AiModelsResponse)
def read_saved_ai_provider_models(profile_id: int) -> dict:
    profile = get_ai_provider_profile(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="AI provider profile not found.")
    settings = _profile_settings(profile)
    provider = selected_provider(settings)
    try:
        return {"ok": True, "provider": provider, "models": fetch_provider_models(settings), "error": None}
    except AiProviderError as exc:
        return {"ok": False, "provider": provider, "models": [], "error": str(exc)}


@router.post("/ai/test", response_model=AiProviderTestResponse)
def test_ai_provider_settings(
    payload: AiSettingsPayload | None = Body(default=None),
) -> dict:
    settings = _resolve_settings(payload)
    return test_provider(settings)


@router.post("/ai/models", response_model=AiModelsResponse)
def read_ai_provider_models(payload: AiSettingsPayload | None = Body(default=None)) -> dict:
    settings = _resolve_settings(payload)
    provider = selected_provider(settings)
    try:
        models = fetch_provider_models(settings)
        return {"ok": True, "provider": provider, "models": models, "error": None}
    except AiProviderError as exc:
        return {"ok": False, "provider": provider, "models": [], "error": str(exc)}


def _resolve_settings(payload: AiSettingsPayload | None) -> dict:
    existing = get_ai_settings()
    if payload is None:
        return existing
    submitted = payload.model_dump()
    existing_keys = existing.get("api_keys") or {}
    submitted_keys = submitted.get("api_keys") or {}
    submitted["api_keys"] = {
        provider: sanitize_secret_input(
            submitted_keys.get(provider),
            existing_keys.get(provider, ""),
        )
        for provider in AI_PROVIDERS
    }
    return submitted
