from __future__ import annotations
from contextlib import closing

from fastapi import APIRouter, Depends, HTTPException

from app.database import get_connection, init_db
from app.models.ai_provider_settings import (
    AIModelSlotSettings,
    AIModelSlotTestDraft,
    AIProviderSettingsResponse,
    AIProviderTaskSettings,
    AIProviderTaskTestResponse,
    AIProviderTaskUpdate,
    AITaskType,
)
from app.routers.integration_gateway import _contributions
from app.security import require_admin
from app.services.application_logging import log_event
from app.services.ai_task_provider import (
    get_provider_for_task,
    list_task_providers_masked,
    mask_task_provider,
    resolve_provider_api_key,
    save_provider_for_task,
    task_provider_ai_settings,
)
from app.services.ai_provider import AiProviderError, fetch_provider_models, test_provider
from app.services.codex_provider import CodexProvider, CodexProviderError
from app.services.codex_usage import clear_quota_until, usage_status
from app.services.gemini_cli_provider import GeminiCliProvider
from app.services.model_slots import (
    active_model_slots,
    get_slot_route,
    models_for_kind,
    profile_id_of,
    resolve_model_slot,
    save_slot_route,
    saved_provider_settings,
    saved_providers,
)


router = APIRouter(
    prefix="/api/ai/providers",
    tags=["ai-provider-settings"],
    dependencies=[Depends(require_admin)],
)


@router.get("", response_model=AIProviderSettingsResponse)
def read_ai_task_providers() -> dict:
    init_db()
    with closing(get_connection()) as db, db:
        saved = saved_providers(db)
        slots = [
            _slot_view(slot, mask_task_provider(get_slot_route(db, slot["integration_id"], slot["slot_id"])), saved)
            for slot in active_model_slots(_contributions)
        ]
        return {"providers": list_task_providers_masked(db), "slots": slots, "saved_providers": saved}


def _slot_view(slot: dict, route: dict, saved: list[dict]) -> dict:
    """A declared slot merged with its stored route and its saved provider's name."""
    profile_id = profile_id_of(str(route.get("provider") or ""))
    fallback_profile_id = profile_id_of(str(route.get("fallback_provider") or ""))
    names = {item["id"]: item["name"] for item in saved}
    return {
        **slot, **route,
        "saved_provider_name": names.get(profile_id, "") if profile_id else "",
        "fallback_saved_provider_name": names.get(fallback_profile_id, "") if fallback_profile_id else "",
    }


def _declared_slot(integration_id: str, slot_id: str) -> dict:
    for slot in active_model_slots(_contributions):
        if slot["integration_id"] == integration_id and slot["slot_id"] == slot_id:
            return slot
    raise HTTPException(status_code=404, detail="No active integration declares this model slot.")


@router.get("/codex/auth")
def read_codex_auth() -> dict:
    try:
        result = CodexProvider.auth_status({})
    except CodexProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    usage = usage_status()
    result["usage"] = usage
    result["quota_until"] = usage.get("quota_until") or result.get("quota_until") or ""
    result["quota_state"] = "reached" if result["quota_until"] else "available"
    return result


@router.get("/gemini-cli/status")
def read_gemini_cli_status() -> dict:
    try:
        return GeminiCliProvider.health({})
    except CodexProviderError as exc:
        return {
            "ok": False,
            "enabled": False,
            "authenticated": None,
            "account_status": "not_exposed_by_cli",
            "error": str(exc),
            "details": dict(exc.context),
        }


@router.post("/codex/auth/device")
def begin_codex_device_login() -> dict:
    try:
        flow = CodexProvider.begin_device_login({})
    except CodexProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    log_event(
        level="info", category="security", source="ai_provider.codex_auth",
        message="Codex device login started.", details={"action": "login_started"},
    )
    return flow


@router.get("/codex/auth/device/{flow_id}")
def read_codex_device_login(flow_id: str) -> dict:
    try:
        state = CodexProvider.device_login_status(flow_id, {})
    except CodexProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if str(state.get("state") or "") == "completed":
        clear_quota_until()
    return state


@router.post("/codex/auth/logout")
def logout_codex() -> dict:
    try:
        result = CodexProvider.logout({})
    except CodexProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    clear_quota_until()
    log_event(
        level="info", category="security", source="ai_provider.codex_auth",
        message="Codex account logged out.", details={"action": "logout"},
    )
    usage = usage_status()
    result["usage"] = usage
    result["quota_until"] = usage.get("quota_until") or ""
    result["quota_state"] = "reached" if result["quota_until"] else "available"
    return result


@router.put("/slots/{integration_id}/{slot_id}", response_model=AIModelSlotSettings)
def update_ai_model_slot(
    integration_id: str, slot_id: str, payload: AIProviderTaskUpdate,
) -> dict:
    slot = _declared_slot(integration_id, slot_id)
    init_db()
    try:
        with closing(get_connection()) as db, db:
            saved = save_slot_route(
                db,
                slot,
                provider=payload.provider,
                protocol=payload.protocol,
                model=payload.model,
                credential_mode=payload.credential_mode,
                api_key_env_name=payload.api_key_env_name,
                api_key_value=payload.api_key_value,
                credential_ref=payload.credential_ref,
                base_url=payload.base_url,
                enabled=payload.enabled,
                fallback_provider=payload.fallback_provider,
                fallback_model=payload.fallback_model,
                reasoning_effort=payload.reasoning_effort,
            )
            view = _slot_view(slot, saved, saved_providers(db))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return view


@router.post("/slots/{integration_id}/{slot_id}/test", response_model=AIProviderTaskTestResponse)
def test_ai_model_slot(
    integration_id: str, slot_id: str, draft: AIModelSlotTestDraft | None = None,
) -> dict:
    """Test the slot and return the models that suit its kind.

    A saved provider selected on screen is tested before it is saved: picking
    one and pressing Test (which also loads its model list) is the natural
    order, and refusing it as "not configured" sent operators in circles. A
    manually entered provider is tested only once saved, because its key is
    never sent back from the screen.
    """
    slot = _declared_slot(integration_id, slot_id)
    init_db()
    try:
        with closing(get_connection()) as db, db:
            draft_profile = profile_id_of(draft.provider) if draft else None
            if draft_profile is not None:
                settings = saved_provider_settings(
                    db, draft_profile, kind=slot["kind"], model=draft.model, label=slot["label"],
                )
                result = _run_slot_test(settings, kind=slot["kind"])
                result["models"] = models_for_kind(result["models"], slot["kind"])
                return result
            provider = get_slot_route(db, integration_id, slot_id)
            if profile_id_of(str(provider.get("provider") or "")) is not None:
                # A saved-provider slot resolves through the profile, so a missing
                # or disabled profile is refused here rather than tested as-is.
                settings = resolve_model_slot(
                    db, integration_id=integration_id, slot_id=slot_id,
                    kind=slot["kind"], label=slot["label"],
                )
                result = _run_slot_test(settings, kind=slot["kind"])
            else:
                result = _test_route(
                    provider, kind=slot["kind"],
                    disabled_message="This model slot is not saved yet. Save it first, then test.",
                )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result["models"] = models_for_kind(result["models"], slot["kind"])
    return result


@router.put("/{task_type}", response_model=AIProviderTaskSettings)
def update_ai_task_provider(
    task_type: AITaskType, payload: AIProviderTaskUpdate,
) -> dict:
    init_db()
    try:
        with closing(get_connection()) as db, db:
            return save_provider_for_task(
                db,
                task_type=task_type,
                provider=payload.provider,
                protocol=payload.protocol,
                model=payload.model,
                credential_mode=payload.credential_mode,
                api_key_env_name=payload.api_key_env_name,
                api_key_value=payload.api_key_value,
                credential_ref=payload.credential_ref,
                base_url=payload.base_url,
                enabled=payload.enabled,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{task_type}/test", response_model=AIProviderTaskTestResponse)
def test_ai_task_provider(task_type: AITaskType) -> dict:
    """Test the persisted route and return its currently available models."""
    init_db()
    try:
        with closing(get_connection()) as db, db:
            provider = get_provider_for_task(db, task_type)
        return _test_route(provider, disabled_message="This task route is disabled.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _test_route(provider: dict, *, disabled_message: str, kind: str = "text") -> dict:
    provider_name = str(provider.get("provider") or "mock")
    if not provider.get("enabled") or provider_name == "disabled":
        raise ValueError(disabled_message)
    if provider_name not in {"mock", "default", "codex"}:
        resolve_provider_api_key(provider)
    settings = task_provider_ai_settings(provider)
    if kind == "image":
        return _run_slot_test(settings, kind=kind)
    return _run_test(
        settings, provider_name=provider_name,
        protocol=str(provider.get("protocol") or "openai_chat"),
        model=str(provider.get("model") or ""),
    )


def _run_slot_test(settings: dict, *, kind: str) -> dict:
    """Test a slot's provider the way that slot will use it.

    A text slot runs the ordinary test with its own model. An
    image slot cannot: before a picture model is chosen there is no model, and
    after, the ordinary test would send a text request to an image model. So an
    image slot is tested by listing the account's models with the saved key --
    which proves the key and address work, needs no model, and fills the list
    the operator picks the picture model from.
    """
    provider_name = str(settings.get("provider") or "")
    protocol = str(settings.get("protocol") or "openai_chat")
    model = str(settings.get("model") or "")
    if kind != "image":
        return _run_test(settings, provider_name=provider_name, protocol=protocol, model=model)
    try:
        models = fetch_provider_models(settings)
    except AiProviderError as exc:
        return {"ok": False, "provider": provider_name, "protocol": protocol, "model": model,
                "models": [], "error": str(exc)}
    result = {"ok": True, "provider": provider_name, "protocol": protocol, "model": model,
              "models": models, "error": None}
    return _with_codex_quota(result, provider_name)


def _run_test(settings: dict, *, provider_name: str, protocol: str, model: str) -> dict:
    result = test_provider(settings)
    models: list[dict] = []
    if result.get("ok"):
        try:
            models = fetch_provider_models(settings)
        except AiProviderError:
            # A successful configured-model test is authoritative; model
            # discovery is optional for compatible endpoints.
            models = []
    response = {
        "ok": bool(result.get("ok")), "provider": provider_name,
        "protocol": protocol, "model": model, "models": models,
        "error": result.get("error"),
    }
    for key in ("authenticated", "plan", "quota_state", "quota_until"):
        if result.get(key) not in (None, ""):
            response[key] = result[key]
    return _with_codex_quota(response, provider_name)


def _with_codex_quota(response: dict, provider_name: str) -> dict:
    if provider_name == "codex":
        usage = usage_status()
        response["quota_state"] = "reached" if usage.get("quota_until") else response.get("quota_state") or "available"
        response["quota_until"] = usage.get("quota_until") or response.get("quota_until") or ""
    return response
