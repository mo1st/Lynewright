"""Model slots: models an integration declares it needs, picked by the operator.

An integration lists its slots on a ``provides`` entry of its manifest. Core
discovers them from the active integrations the same way it discovers
operations, and AI provider settings shows one picker per slot. A slot's route
lives in ``ai_task_providers`` under the key ``slot:<integration_id>:<slot_id>``,
so it cannot collide with a built-in task type and needs no migration.

There is no default. A slot with no stored row, a disabled row, or a
``disabled``/``mock``/empty provider is not configured.

A slot may also use a saved provider (one row of ``ai_provider_profiles``,
managed in AI & Providers). The row then stores ``provider = "profile:<id>"``
and no credential at all: the profile is read at resolve time, so a key rotated
there reaches the slot, and no key is ever copied into the slot.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any

from app.database import _ai_profile_as_settings
from app.services.ai_task_provider import (
    _read_route,
    _write_route,
    mask_task_provider,
    task_provider_ai_settings,
)


_NOT_CONFIGURED_PROVIDERS = {"", "disabled", "mock"}
_IMAGE_MODEL_RE = re.compile(r"image|dall-e|imagen|gpt-image", re.IGNORECASE)
_VISION_MODEL_RE = re.compile(r"vl|vision", re.IGNORECASE)
PROFILE_PREFIX = "profile:"
_PROFILE_RE = re.compile(r"^profile:([1-9][0-9]*)$")
# Providers that authenticate without a stored key.
_KEYLESS_PROVIDERS = {"codex", "gemini_cli", "mock"}


class ModelSlotNotConfigured(ValueError):
    pass


def slot_key(integration_id: str, slot_id: str) -> str:
    return f"slot:{integration_id}:{slot_id}"


def profile_id_of(provider: str) -> int | None:
    """The saved-provider id when ``provider`` is ``profile:<id>``, else None."""
    match = _PROFILE_RE.fullmatch(str(provider or "").strip())
    return int(match.group(1)) if match else None


def _load_profile(db: sqlite3.Connection, profile_id: int) -> dict[str, Any] | None:
    cursor = db.execute("SELECT * FROM ai_provider_profiles WHERE id = ?", (profile_id,))
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    return dict(zip([column[0] for column in cursor.description], row))


def saved_providers(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """The saved providers a slot can pick. Names and models only, never a key."""
    rows = db.execute(
        "SELECT id, name, provider, model, enabled FROM ai_provider_profiles ORDER BY priority ASC, id ASC"
    ).fetchall()
    return [
        {"id": int(row[0]), "name": str(row[1]), "provider": str(row[2]),
         "model": str(row[3] or ""), "enabled": bool(row[4])}
        for row in rows
    ]


def discover_model_slots(contributions: Any) -> list[dict[str, Any]]:
    """Enumerate the model slots declared by activated integrations."""
    found: dict[str, dict[str, Any]] = {}
    for contribution in contributions.list(kind="service"):
        metadata = getattr(contribution, "metadata", None) or {}
        declared = metadata.get("model_slots")
        if not isinstance(declared, list):
            continue
        integration_id = str(getattr(contribution, "integration_id", "") or "")
        integration_name = str(metadata.get("integration_name") or integration_id)
        for slot in declared:
            if not isinstance(slot, dict) or not slot.get("id"):
                continue
            key = slot_key(integration_id, str(slot["id"]))
            if key in found:
                continue
            found[key] = {
                "slot_key": key,
                "integration_id": integration_id,
                "integration_name": integration_name,
                "slot_id": str(slot["id"]),
                "kind": str(slot.get("kind") or "text"),
                "label": str(slot.get("label") or slot["id"]),
                "description": str(slot.get("description") or ""),
            }
    return sorted(found.values(), key=lambda item: (item["integration_name"], item["label"]))


def active_model_slots(contributions: Any) -> list[dict[str, Any]]:
    """The slots of the active integrations, from the registry the caller holds.

    The registry lives in a router module, and a service must not import a router,
    so the caller passes it in. ``None`` (no registry available) means no slots.
    """
    slots = discover_model_slots(contributions) if contributions is not None else []
    key = slot_key("lynewright.core", "integration_drafter")
    if not any(item.get("slot_key") == key for item in slots):
        slots.append({
            "slot_key": key,
            "integration_id": "lynewright.core",
            "integration_name": "Lynewright",
            "slot_id": "integration_drafter",
            "kind": "text",
            "label": "Integration drafter",
            "description": "Drafts declarative integration files from operator supplied documentation.",
        })
    return sorted(slots, key=lambda item: (item["integration_name"], item["label"]))


def get_slot_route(db: sqlite3.Connection, integration_id: str, slot_id: str) -> dict[str, Any]:
    stored = _read_route(db, slot_key(integration_id, slot_id))
    if stored is not None:
        return stored
    return {
        "provider": "disabled",
        "protocol": "openai_chat",
        "model": "",
        "credential_mode": "raw_key",
        "api_key_env_name": "",
        "credential_ref": "",
        "api_key_value": "",
        "base_url": "",
        "fallback_provider": "",
        "fallback_model": "",
        "enabled": False,
    }


def save_slot_route(
    db: sqlite3.Connection,
    slot: dict[str, Any],
    *,
    provider: str,
    model: str,
    credential_mode: str,
    api_key_env_name: str,
    api_key_value: str | None,
    base_url: str,
    enabled: bool,
    protocol: str | None = None,
    credential_ref: str | None = None,
    fallback_provider: str = "",
    fallback_model: str = "",
    reasoning_effort: str = "",
) -> dict[str, Any]:
    key = slot_key(slot["integration_id"], slot["slot_id"])
    profile_id = profile_id_of(provider)
    _validate_fallback(db, slot, fallback_provider, fallback_model)
    if profile_id is not None:
        return _save_profile_route(
            db, slot, key, profile_id, model=model, enabled=enabled,
            fallback_provider=fallback_provider, fallback_model=fallback_model,
        )
    _write_route(
        db, key,
        provider=provider, protocol=protocol, model=model,
        credential_mode=credential_mode, api_key_env_name=api_key_env_name,
        api_key_value=api_key_value, credential_ref=credential_ref,
        base_url=base_url, enabled=enabled,
        fallback_provider=fallback_provider, fallback_model=fallback_model,
        reasoning_effort=reasoning_effort,
    )
    return mask_task_provider(get_slot_route(db, slot["integration_id"], slot["slot_id"]))


def _save_profile_route(
    db: sqlite3.Connection, slot: dict[str, Any], key: str, profile_id: int,
    *, model: str, enabled: bool, fallback_provider: str = "", fallback_model: str = "",
) -> dict[str, Any]:
    profile = _load_profile(db, profile_id)
    if profile is None:
        raise ValueError("That saved provider no longer exists.")
    name = str(profile.get("name") or f"#{profile_id}")
    is_image = slot["kind"] == "image"
    if is_image:
        if not model.strip():
            raise ValueError(f"Type the image model to use with {name}.")
    _write_route(
        db, key,
        provider=f"{PROFILE_PREFIX}{profile_id}", protocol=None,
        # A text slot uses the profile's own model; an image slot names its own.
        model=model.strip() if is_image else "",
        credential_mode="raw_key", api_key_env_name="", api_key_value="",
        credential_ref="", base_url="", enabled=enabled,
        fallback_provider=fallback_provider, fallback_model=fallback_model,
    )
    # _write_route keeps a previously stored key and credential reference when
    # the new ones are blank (so editing other fields never wipes a secret).
    # A slot switched from a manual route to a saved provider must not keep the
    # old secret behind the profile it now points at.
    db.execute(
        """UPDATE ai_task_providers
           SET api_key_value = '', credential_ref = '', api_key_env_name = '', base_url = ''
           WHERE task_type = ?""",
        (key,),
    )
    db.commit()
    return mask_task_provider(get_slot_route(db, slot["integration_id"], slot["slot_id"]))


def _profile_settings(
    db: sqlite3.Connection, profile_id: int, *, kind: str, image_model: str,
) -> dict[str, Any]:
    """Settings built from a saved provider, with no fallback to anything else."""
    profile = _load_profile(db, profile_id)
    name = str((profile or {}).get("name") or f"#{profile_id}")
    unavailable = ModelSlotNotConfigured(f"Saved provider {name} is unavailable.")
    if profile is None or not profile.get("enabled"):
        raise unavailable
    settings = _ai_profile_as_settings(profile)
    provider = str(settings.get("provider") or "")
    if provider not in _KEYLESS_PROVIDERS and not (settings.get("api_keys") or {}).get(provider):
        raise unavailable
    # A saved-provider slot never falls back: the app-wide profile chain is what
    # ordinary task routes silently fall through to.
    settings["fallback_chain"] = []
    if kind == "image":
        settings["model"] = image_model
    return settings


def resolve_model_slot(
    db: sqlite3.Connection, *, integration_id: str, slot_id: str, kind: str, label: str = "",
) -> dict[str, Any]:
    """Settings for the model an operator picked for this slot, or a clear refusal."""
    not_configured = ModelSlotNotConfigured(
        f"The model for '{label or slot_id}' is not configured. Pick one in AI provider settings."
    )
    route = get_slot_route(db, integration_id, slot_id)
    if not route.get("enabled") or str(route.get("provider") or "").strip() in _NOT_CONFIGURED_PROVIDERS:
        raise not_configured
    profile_id = profile_id_of(str(route.get("provider") or ""))
    if profile_id is not None:
        primary = saved_provider_settings(
            db, profile_id, kind=kind, model=str(route.get("model") or ""), label=label or slot_id,
        )
    else:
        primary = task_provider_ai_settings(route, connection=db)
    # A route may inherit the app-wide provider, so the adapter check applies to
    # what it resolves to, not to the literal ``default`` marker.
    fallback = _resolve_fallback(
        db, route, kind=kind, label=label or slot_id,
    )
    # Keep the primary settings at top level for callers that predate fallback
    # routing, and expose both explicit branches for new callers and diagnostics.
    return {**primary, "primary": dict(primary), "fallback": fallback, "_slot_fallback": fallback}


def _validate_fallback(
    db: sqlite3.Connection, slot: dict[str, Any], fallback_provider: str, fallback_model: str,
) -> None:
    selected = str(fallback_provider or "").strip()
    if not selected:
        return
    profile_id = profile_id_of(selected)
    if profile_id is None:
        raise ValueError("Choose a saved provider or None for the slot fallback.")
    profile = _load_profile(db, profile_id)
    if profile is None:
        raise ValueError("That fallback provider no longer exists.")
    provider = str(profile.get("provider") or "")
    if slot["kind"] == "image":
        if provider == "codex":
            raise ValueError("Choose a non-Codex image provider as the fallback.")
        if not provider_has_adapter(provider):
            raise ValueError("The fallback provider cannot make images.")
        if not str(fallback_model or "").strip():
            raise ValueError("Choose an image model for the fallback provider.")


def _resolve_fallback(
    db: sqlite3.Connection, route: dict[str, Any], *, kind: str, label: str,
) -> dict[str, Any] | None:
    provider = str(route.get("fallback_provider") or "").strip()
    profile_id = profile_id_of(provider)
    if profile_id is None:
        return None
    profile = _load_profile(db, profile_id)
    if profile is None or not profile.get("enabled"):
        return None
    settings = saved_provider_settings(
        db, profile_id, kind=kind, model=str(route.get("fallback_model") or ""), label=label,
    )
    settings["fallback_chain"] = []
    settings["provider_used"] = settings.get("provider")
    return settings


_IMAGE_CAPABLE = "OpenAI, OpenAI-compatible, Gemini or Codex"


def saved_provider_settings(
    db: sqlite3.Connection, profile_id: int, *, kind: str, model: str, label: str,
) -> dict[str, Any]:
    """Settings for a saved provider used by a slot, saved or only selected on screen.

    Shared by resolution and by the settings screen's Test / load-models button,
    so testing what is selected -- before saving -- checks exactly what a real
    call would use.
    """
    settings = _profile_settings(db, profile_id, kind=kind, image_model=model)
    provider = str(settings.get("provider") or "")
    if kind == "image" and not provider_has_adapter(provider):
        raise ModelSlotNotConfigured(
            f"The saved provider for '{label}' uses {provider}, which cannot make pictures. "
            f"Choose a saved {_IMAGE_CAPABLE} provider."
        )
    return settings


def models_for_kind(models: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    """Narrow a provider's model list to the ones that suit a slot's kind.

    Model lists carry no text/image flag, so this goes by name. When the filter
    would leave nothing, the full list is returned so a provider with unusual
    names never shows an empty picker.
    """
    def is_image(model: dict[str, Any]) -> bool:
        return bool(_IMAGE_MODEL_RE.search(str(model.get("id") or "")))

    def is_vision(model: dict[str, Any]) -> bool:
        return bool(_VISION_MODEL_RE.search(str(model.get("id") or "")))

    if kind == "vision":
        selected = [model for model in models if is_vision(model)]
        return selected or list(models)
    wants_image = kind == "image"
    selected = [model for model in models if is_image(model) == wants_image]
    return selected or list(models)

