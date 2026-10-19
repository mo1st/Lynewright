"""Safe boundary for the official OpenAI Codex Python SDK.

The provider is deliberately optional.  Lynewright owns schemas, permissions,
approvals and execution; Codex is only asked for text/JSON decisions.  The
SDK is started per bounded call so a thread cannot leak context between users
or runs.  Production deployments keep this disabled until a protected Codex
runtime (with its own authenticated session) is explicitly configured.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import hashlib
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from app.services.internal_endpoints import pinned_url, resolve_internal_endpoint


# Lynewright's Codex route is deliberately cost-bounded and deterministic. The
# dashboard may choose bounded model/effort settings from the declarative
# integration schema; task payloads still cannot supply arbitrary CLI flags,
# commands, paths, or credentials.
CODEX_MODEL = "gpt-5.6-luna"
CODEX_REASONING_EFFORT = "high"
_ALLOWED_REASONING_EFFORTS = frozenset({"minimal", "low", "medium", "high", "xhigh", "max", "ultra"})
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


class CodexProviderError(RuntimeError):
    """A safe, classified failure from the optional Codex runtime."""

    def __init__(self, message: str, *, failure_kind: str = "unavailable", **context: Any) -> None:
        super().__init__(message)
        self.context = {"failure_kind": failure_kind, **{k: v for k, v in context.items() if v not in (None, "")}}


def _failure_from_state(state: dict[str, Any], *, default: str) -> CodexProviderError:
    message = " ".join(str(state.get("error") or default).split())[:300]
    kind = str(state.get("failure_kind") or "runner_failed").strip().lower()
    if kind not in {"quota", "auth", "timeout", "runner_failed", "daily_cap"}:
        kind = "runner_failed"
    context: dict[str, Any] = {}
    retry_after = str(state.get("retry_after") or "").strip() or _retry_after(message)
    if retry_after:
        context["retry_after"] = retry_after
        if str(state.get("provider") or "codex") == "codex":
            try:
                from app.services.codex_usage import cache_quota_until

                cache_quota_until(retry_after)
            except Exception:
                # Usage accounting must never hide the provider's actual failure.
                pass
    return CodexProviderError(message, failure_kind=kind, **context)


def _reserve_usage(kind: str, idempotency_key: str, *, provider: str = "codex") -> None:
    from app.services.codex_usage import CodexUsageLimitError, reserve_call

    try:
        reserve_call(kind, idempotency_key, provider=provider)
    except CodexUsageLimitError as exc:
        context = {"retry_after": exc.retry_after} if exc.retry_after else {}
        raise CodexProviderError(str(exc), failure_kind=exc.failure_kind, **context) from exc


def _retry_after(message: str) -> str:
    match = re.search(
        r"try again at\s+([A-Za-z]{3,9}\s+\d{1,2}(?:st|nd|rd|th)?,\s+\d{4})",
        str(message or ""), re.IGNORECASE,
    )
    if not match:
        return ""
    value = re.sub(r"(?<=\d)(?:st|nd|rd|th)", "", match.group(1), flags=re.IGNORECASE)
    for pattern in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(value, pattern).replace(tzinfo=timezone.utc).isoformat(timespec="seconds")
        except ValueError:
            continue
    return ""


def _truthy(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _setting(settings: dict[str, Any], key: str, env_name: str, default: str = "") -> str:
    value = str(settings.get(key) or "").strip()
    return value or os.getenv(env_name, default).strip()


def is_enabled(settings: dict[str, Any] | None = None) -> bool:
    settings = settings or {}
    explicit = settings.get("codex_enabled")
    if explicit is not None:
        return bool(explicit)
    return _truthy(os.getenv("LYNEWRIGHT_CODEX_ENABLED"), False)


def _bridge_enabled(settings: dict[str, Any]) -> bool:
    explicit = settings.get("codex_runner_enabled")
    if explicit is not None:
        return bool(explicit)
    return _truthy(os.getenv("LYNEWRIGHT_CODEX_RUNNER_ENABLED"), False)


def _bridge_origin(settings: dict[str, Any]) -> str:
    ref = _setting(settings, "codex_runner_endpoint_ref", "LYNEWRIGHT_CODEX_RUNNER_ENDPOINT_REF", "codex_runner")
    try:
        return resolve_internal_endpoint(ref)
    except Exception as exc:
        raise CodexProviderError(
            "The protected Codex runner endpoint is not configured.",
            failure_kind="configuration",
        ) from exc


def _bridge_token(settings: dict[str, Any]) -> str:
    token_env = _setting(settings, "codex_runner_token_env", "LYNEWRIGHT_CODEX_RUNNER_TOKEN_ENV", "LYNEWRIGHT_CODEX_RUNNER_TOKEN")
    token = os.getenv(token_env, "").strip()
    if not token:
        raise CodexProviderError("The protected runner is not configured.", failure_kind="configuration")
    return token


def _runner_cli_backend(settings: dict[str, Any]) -> str:
    """Resolve the selected protected runner backend."""
    merged = _runner_config(settings)
    value = str(merged.get("codex_runner_cli_backend") or merged.get("cli_backend") or "").strip().lower()
    return value if value in {"codex", "opencode", "gemini"} else "codex"


def _runner_config(settings: dict[str, Any]) -> dict[str, Any]:
    """Read explicit settings supplied for this provider invocation."""
    merged: dict[str, Any] = {}
    for key in (
        "codex_runner_cli_backend",
        "cli_backend",
        "cli_model",
        "cli_reasoning_effort",
        "cli_api_key",
        "cli_api_key_env",
        "cli_timeout_seconds",
    ):
        if settings.get(key) not in (None, ""):
            merged[key] = settings[key]
    # A model slot routed to a CLI provider carries the operator's model as a
    # plain value; it wins over the provider defaults. Gemini's empty value
    # deliberately clears a Codex model override and leaves its CLI default.
    provider = str(settings.get("provider") or "")
    if provider in {"codex", "gemini_cli"}:
        configured_backend = str(merged.get("codex_runner_cli_backend") or merged.get("cli_backend") or "").strip().lower()
        if provider == "gemini_cli":
            merged["codex_runner_cli_backend"] = "gemini"
            # This profile authenticates inside the official CLI. Never forward
            # an unrelated stored CLI API-key credential with its runner jobs.
            merged.pop("cli_api_key", None)
            merged.pop("cli_api_key_env", None)
        elif configured_backend == "gemini":
            # A Codex profile does not inherit another provider's CLI choice.
            merged["codex_runner_cli_backend"] = "codex"
        slot_model = str(settings.get("model") or "").strip()
        if provider == "gemini_cli":
            merged["cli_model"] = slot_model if slot_model and _MODEL_RE.fullmatch(slot_model) else ""
        elif slot_model and slot_model != "mock-brief-v1" and _MODEL_RE.fullmatch(slot_model):
            merged["cli_model"] = slot_model
        if provider == "codex":
            slot_effort = str(settings.get("reasoning_effort") or "").strip().lower()
            if slot_effort in _ALLOWED_REASONING_EFFORTS:
                merged["cli_reasoning_effort"] = slot_effort
    configured_backend = str(merged.get("codex_runner_cli_backend") or merged.get("cli_backend") or "").strip().lower()
    if configured_backend == "gemini" and provider != "gemini_cli":
        configured_model = os.getenv("CODEX_GEMINI_MODEL", "").strip()
        merged["cli_model"] = configured_model if configured_model and _MODEL_RE.fullmatch(configured_model) else ""
    return merged


def _runner_model(settings: dict[str, Any]) -> str:
    backend = _runner_cli_backend(settings)
    defaults = {"codex": CODEX_MODEL, "opencode": "opencode/big-pickle", "gemini": "auto"}
    value = str(_runner_config(settings).get("cli_model") or defaults.get(backend, CODEX_MODEL)).strip()
    if not _MODEL_RE.fullmatch(value):
        return defaults.get(backend, CODEX_MODEL)
    return value


def _runner_effort(settings: dict[str, Any]) -> str:
    value = str(_runner_config(settings).get("cli_reasoning_effort") or CODEX_REASONING_EFFORT).strip().lower()
    return value if value in _ALLOWED_REASONING_EFFORTS else CODEX_REASONING_EFFORT


def _runner_timeout(settings: dict[str, Any], default: int, maximum: int) -> int:
    value = _runner_config(settings).get("cli_timeout_seconds")
    try:
        seconds = int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        seconds = default
    return max(10, min(seconds, maximum))


def _bridge_request(settings: dict[str, Any], path: str, *, payload: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
    """Call the deployment-pinned runner without accepting a provider URL."""
    url = pinned_url(_bridge_origin(settings), path)
    headers = {"Accept": "application/json", "Authorization": f"Bearer {_bridge_token(settings)}"}
    runner_config = _runner_config(settings)
    data = None
    if payload is not None:
        payload = dict(payload)
        if path.startswith("/v1/codex/"):
            payload.setdefault("cli_backend", _runner_cli_backend(settings))
        if path in {"/v1/codex/structured", "/v1/jobs", "/v1/codex/image"}:
            for key in ("cli_model", "cli_reasoning_effort", "cli_api_key_env"):
                if path == "/v1/codex/image" and key == "cli_api_key_env":
                    continue
                value = runner_config.get(key)
                if value not in (None, ""):
                    payload.setdefault(key, value)
            if runner_config.get("cli_timeout_seconds") not in (None, ""):
                if path == "/v1/codex/structured":
                    payload.setdefault("cli_timeout_seconds", _runner_timeout(settings, default=180, maximum=900))
                else:
                    payload.setdefault("cli_timeout_seconds", runner_config["cli_timeout_seconds"])
            # The secret travels only in an internal header and is never put
            # into request JSON or persisted job state.
            if path == "/v1/codex/structured" and runner_config.get("cli_api_key"):
                headers["X-Lynewright-CLI-API-Key"] = str(runner_config["cli_api_key"])
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise CodexProviderError("The protected Codex runner rejected the request.", failure_kind="runner_http") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise CodexProviderError("The protected Codex runner is unavailable.", failure_kind="runner_unavailable") from exc
    if not isinstance(value, dict):
        raise CodexProviderError("The protected Codex runner returned an invalid response.", failure_kind="invalid_output")
    return value


def _bridge_binary(settings: dict[str, Any], path: str, *, timeout: float = 60.0, max_bytes: int = 20 * 1024 * 1024) -> tuple[bytes, str]:
    """Fetch a bounded image artifact from the protected runner."""
    url = pinned_url(_bridge_origin(settings), path)
    request = urllib.request.Request(
        url,
        headers={"Accept": "image/png,image/jpeg,image/webp", "Authorization": f"Bearer {_bridge_token(settings)}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content_type = str(response.headers.get("Content-Type") or "application/octet-stream").split(";", 1)[0].strip()
            content = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        raise CodexProviderError("The protected Codex image runner rejected the request.", failure_kind="runner_http") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CodexProviderError("The protected Codex image runner is unavailable.", failure_kind="runner_unavailable") from exc
    if len(content) > max_bytes:
        raise CodexProviderError("The protected Codex image artifact exceeded the bounded size.", failure_kind="invalid_output")
    if content_type not in {"image/png", "image/jpeg", "image/webp"}:
        raise CodexProviderError("The protected Codex runner returned a non-image artifact.", failure_kind="invalid_output")
    return content, content_type


def _bridge_health(settings: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    value = _bridge_request(settings, "/health", timeout=10)
    if value.get("status") != "ok":
        raise CodexProviderError("The protected Codex runner health check failed.", failure_kind="runner_unavailable")
    result = {
        "ok": True,
        "enabled": True,
        "authenticated": True,
        "mode": "protected_runner",
        "model": _runner_model(settings),
        "reasoning_effort": _runner_effort(settings),
        "latency_ms": round((time.monotonic() - started) * 1000),
    }
    # Auth details are returned only by the token-protected status endpoint.
    # Older runner versions may not implement it yet, so a missing endpoint
    # does not turn a healthy existing deployment into a false outage.
    try:
        account = _bridge_request(settings, "/v1/codex/auth", timeout=10)
    except CodexProviderError:
        account = {}
    if "logged_in" in account:
        result["authenticated"] = bool(account.get("logged_in"))
        for source, target in (("plan", "plan"), ("quota_state", "quota_state"), ("quota_until", "quota_until")):
            value = account.get(source)
            if isinstance(value, str) and value.strip():
                result[target] = value.strip()[:120]
        if not result["authenticated"]:
            result["ok"] = False
            result["error"] = "Codex is not logged in. Log in from AI provider settings."
    return result


def _bridge_generate(
    settings: dict[str, Any], *, system_prompt: str, user_prompt: str, json_schema: dict[str, Any] | None,
    images: list[dict[str, str]] | None = None,
) -> tuple[str, dict[str, Any]]:
    if not isinstance(json_schema, dict):
        raise CodexProviderError("The protected Codex bridge requires a JSON schema.", failure_kind="invalid_request")
    started = time.monotonic()
    timeout_seconds = _runner_timeout(
        settings,
        default=int(os.getenv("LYNEWRIGHT_CODEX_STRUCTURED_TIMEOUT_SECONDS", "180")),
        maximum=900,
    )
    runner_config = _runner_config(settings)
    key_material = json.dumps(
        {
            "system": system_prompt,
            "user": user_prompt,
            "schema": json_schema,
            "cli_backend": _runner_cli_backend(settings),
            "cli_model": _runner_model(settings),
            "cli_reasoning_effort": runner_config.get("cli_reasoning_effort") or CODEX_REASONING_EFFORT,
            "cli_timeout_seconds": timeout_seconds,
            "images": [hashlib.sha256(str(item.get("data_base64") or "").encode("ascii")).hexdigest() for item in images or []],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    idempotency_key = "decision_" + hashlib.sha256(key_material.encode("utf-8")).hexdigest()
    usage_provider = "gemini_cli" if str(settings.get("provider") or "") == "gemini_cli" else "codex"
    _reserve_usage("text", idempotency_key, provider=usage_provider)
    created = _bridge_request(
        settings,
        "/v1/codex/structured",
        payload={
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "json_schema": json_schema,
            "timeout_seconds": timeout_seconds,
            "idempotency_key": idempotency_key,
            **({"images": images} if images else {}),
        },
        timeout=30 if images else 15,
    )
    job_id = str(created.get("job_id") or "").strip()
    if not job_id:
        raise CodexProviderError("The protected Codex runner did not return a decision job.", failure_kind="invalid_output")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        state = _bridge_request(settings, f"/v1/codex/structured/{job_id}", timeout=15)
        status = str(state.get("state") or "")
        if status == "completed":
            result = state.get("result")
            text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            # Ensure the provider contract remains JSON even if the bridge is misconfigured.
            json.loads(text)
            usage = {
                "exact": 0,
                "local_runner": 1,
                "latency_ms": int(state.get("latency_ms") or round((time.monotonic() - started) * 1000)),
                "model": state.get("model") or _runner_model(settings),
                "reasoning_effort": state.get("reasoning_effort") or _runner_effort(settings),
            }
            return text, usage
        if status in {"failed", "cancelled"}:
            raise _failure_from_state(state, default="The protected Codex structured decision failed.")
        time.sleep(max(0.2, float(os.getenv("LYNEWRIGHT_CODEX_RUNNER_POLL_SECONDS", "1"))))
    try:
        _bridge_request(settings, f"/v1/codex/structured/{job_id}/cancel", payload={}, timeout=10)
    except CodexProviderError:
        pass
    raise CodexProviderError("The protected Codex structured decision timed out.", failure_kind="timeout")


def _bridge_generate_image(
    settings: dict[str, Any], *, prompt: str, negative_prompt: str = "", style_notes: str = "", size: str = "", timeout_seconds: int = 600,
) -> tuple[bytes, dict[str, Any]]:
    """Use the runner's ChatGPT-authenticated built-in image_gen path."""
    started = time.monotonic()
    key_material = json.dumps(
        {"prompt": prompt, "negative_prompt": negative_prompt, "style_notes": style_notes, "size": size,
         "cli_model": _runner_model(settings), "cli_reasoning_effort": _runner_effort(settings)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    idempotency_key = "image_" + hashlib.sha256(key_material.encode("utf-8")).hexdigest()
    _reserve_usage("images", idempotency_key)
    created = _bridge_request(
        settings,
        "/v1/codex/image",
        payload={
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "style_notes": style_notes,
            "size": size,
            "timeout_seconds": max(30, min(int(timeout_seconds), 900)),
            "idempotency_key": idempotency_key,
        },
        timeout=15,
    )
    job_id = str(created.get("job_id") or "").strip()
    if not job_id:
        raise CodexProviderError("The protected Codex runner did not return an image job.", failure_kind="invalid_output")
    deadline = time.monotonic() + max(30, min(int(timeout_seconds), 900))
    while time.monotonic() < deadline:
        state = _bridge_request(settings, f"/v1/codex/image/{job_id}", timeout=15)
        status = str(state.get("state") or "")
        if status == "completed":
            if state.get("provider") != "codex" or state.get("auth_mode") != "chatgpt_account" or state.get("generation_mode") != "builtin_image_gen":
                raise CodexProviderError("The protected runner returned an unverified image provenance.", failure_kind="invalid_output")
            content, mime_type = _bridge_binary(settings, f"/v1/codex/image/{job_id}/artifact", timeout=60)
            usage = {
                "exact": 0,
                "local_runner": 1,
                "latency_ms": int(state.get("latency_ms") or round((time.monotonic() - started) * 1000)),
                # Protected built-in image_gen remains pinned regardless of
                # editable content/structured CLI settings.
                "model": state.get("model") or _runner_model(settings),
                "reasoning_effort": state.get("reasoning_effort") or _runner_effort(settings),
                "provider": "codex",
                "auth_mode": "chatgpt_account",
                "generation_mode": "builtin_image_gen",
                "mime_type": mime_type,
                "width": state.get("width"),
                "height": state.get("height"),
                "tool_call_observed": bool(state.get("tool_call_observed")),
                "session_output_evidence": bool(state.get("session_output_evidence")),
            }
            return content, usage
        if status in {"failed", "cancelled"}:
            raise _failure_from_state(state, default="The protected Codex image generation failed.")
        time.sleep(max(0.5, float(os.getenv("LYNEWRIGHT_CODEX_RUNNER_POLL_SECONDS", "1"))))
    try:
        _bridge_request(settings, f"/v1/codex/image/{job_id}/cancel", payload={}, timeout=10)
    except CodexProviderError:
        pass
    raise CodexProviderError("The protected Codex image generation timed out.", failure_kind="timeout")


def _bridge_submit_image(
    settings: dict[str, Any], *, prompt: str, negative_prompt: str = "", style_notes: str = "",
    size: str = "", timeout_seconds: int = 600,
) -> dict[str, Any]:
    key_material = json.dumps(
        {"prompt": prompt, "negative_prompt": negative_prompt, "style_notes": style_notes, "size": size,
         "cli_model": _runner_model(settings), "cli_reasoning_effort": _runner_effort(settings)},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    idempotency_key = "image_" + hashlib.sha256(key_material.encode("utf-8")).hexdigest()
    _reserve_usage("images", idempotency_key)
    created = _bridge_request(
        settings,
        "/v1/codex/image",
        payload={
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "style_notes": style_notes,
            "size": size,
            "timeout_seconds": max(30, min(int(timeout_seconds), 900)),
            "idempotency_key": idempotency_key,
        },
        timeout=15,
    )
    job_id = str(created.get("job_id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id):
        raise CodexProviderError("The protected Codex runner did not return an image job.", failure_kind="invalid_output")
    if created.get("state") in {"failed", "cancelled"}:
        raise _failure_from_state(created, default="The protected Codex image generation failed.")
    return {"job_id": job_id, "state": str(created.get("state") or "queued")}


def _bridge_poll_image(settings: dict[str, Any], job_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(job_id or "")):
        raise CodexProviderError("The protected Codex image job id is invalid.", failure_kind="invalid_request")
    state = _bridge_request(settings, f"/v1/codex/image/{job_id}", timeout=15)
    status = str(state.get("state") or "")
    if status in {"failed", "cancelled"}:
        raise _failure_from_state(state, default="The protected Codex image generation failed.")
    if status != "completed":
        return {"job_id": job_id, "state": status or "running"}
    if state.get("provider") != "codex" or state.get("auth_mode") != "chatgpt_account" or state.get("generation_mode") != "builtin_image_gen":
        raise CodexProviderError("The protected runner returned an unverified image provenance.", failure_kind="invalid_output")
    content, mime_type = _bridge_binary(settings, f"/v1/codex/image/{job_id}/artifact", timeout=60)
    return {
        "job_id": job_id,
        "state": "completed",
        "image_bytes": content,
        "mime_type": mime_type,
        "usage": {
            "exact": 0, "local_runner": 1,
            "latency_ms": int(state.get("latency_ms") or 0),
            "model": state.get("model") or _runner_model(settings),
            "reasoning_effort": state.get("reasoning_effort") or _runner_effort(settings),
            "provider": "codex", "auth_mode": "chatgpt_account",
            "generation_mode": "builtin_image_gen", "width": state.get("width"),
            "height": state.get("height"), "tool_call_observed": bool(state.get("tool_call_observed")),
            "session_output_evidence": bool(state.get("session_output_evidence")),
        },
    }


def _bridge_cancel_image(settings: dict[str, Any], job_id: str) -> None:
    if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(job_id or "")):
        _bridge_request(settings, f"/v1/codex/image/{job_id}/cancel", payload={}, timeout=10)


def _load_sdk() -> tuple[Any, Any, Any, Any, Any]:
    try:
        from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox
    except ImportError as exc:  # pragma: no cover - exercised in deployment smoke tests
        raise CodexProviderError(
            "The official openai-codex package is not installed.",
            failure_kind="dependency_missing",
            hint="Install the pinned backend dependency or keep the Codex route disabled.",
        ) from exc
    return Codex, CodexConfig, ApprovalMode, Sandbox, getattr(__import__("openai_codex"), "__version__", "unknown")


def _config(settings: dict[str, Any], *, reasoning: str) -> Any:
    _, CodexConfig, _, _, _ = _load_sdk()
    env: dict[str, str] = {}
    codex_home = _setting(settings, "codex_home", "CODEX_HOME")
    if codex_home:
        env["CODEX_HOME"] = codex_home
    env.setdefault("HF_HUB_OFFLINE", "1")
    env.setdefault("NEEDLE_TELEMETRY", "0")
    overrides = (f'model_reasoning_effort="{reasoning}"',)
    return CodexConfig(
        codex_bin=_setting(settings, "codex_bin", "LYNEWRIGHT_CODEX_BIN") or None,
        config_overrides=overrides,
        cwd=_setting(settings, "codex_cwd", "LYNEWRIGHT_CODEX_CWD") or None,
        env=env or None,
    )


def _usage(result: Any) -> dict[str, Any]:
    usage = getattr(result, "usage", None)
    last = getattr(usage, "last", None)
    if last is None:
        return {}
    data: dict[str, Any] = {}
    for key in ("input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens", "cached_input_tokens"):
        value = getattr(last, key, None)
        if value is not None:
            data[key] = int(value)
    data["exact"] = 1
    return data


class CodexProvider:
    """Adapter that exposes structured Codex decisions to Lynewright."""

    @classmethod
    def auth_status(cls, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        if not is_enabled(settings):
            raise CodexProviderError("Codex provider is disabled.", failure_kind="disabled")
        if not _bridge_enabled(settings or {}):
            raise CodexProviderError("The protected Codex runner is disabled.", failure_kind="configuration")
        return _bridge_request(settings or {}, "/v1/codex/auth", timeout=15)

    @classmethod
    def begin_device_login(cls, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        selected = settings or {}
        if not is_enabled(selected) or not _bridge_enabled(selected):
            raise CodexProviderError("The protected Codex runner is disabled.", failure_kind="configuration")
        return _bridge_request(selected, "/v1/codex/auth/device", payload={}, timeout=20)

    @classmethod
    def device_login_status(cls, flow_id: str, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", str(flow_id or "")):
            raise CodexProviderError("Invalid Codex login flow id.", failure_kind="invalid_request")
        selected = settings or {}
        if not is_enabled(selected) or not _bridge_enabled(selected):
            raise CodexProviderError("The protected Codex runner is disabled.", failure_kind="configuration")
        return _bridge_request(selected, f"/v1/codex/auth/device/{flow_id}", timeout=15)

    @classmethod
    def logout(cls, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        selected = settings or {}
        if not is_enabled(selected) or not _bridge_enabled(selected):
            raise CodexProviderError("The protected Codex runner is disabled.", failure_kind="configuration")
        return _bridge_request(selected, "/v1/codex/auth/logout", payload={}, timeout=30)

    @classmethod
    def health(cls, settings: dict[str, Any]) -> dict[str, Any]:
        if not is_enabled(settings):
            return {"ok": False, "enabled": False, "authenticated": False, "error": "Codex provider is disabled."}
        if _bridge_enabled(settings):
            return _bridge_health(settings)
        started = time.monotonic()
        try:
            Codex, _, _, _, version = _load_sdk()
            with Codex(_config(settings, reasoning=CODEX_REASONING_EFFORT)) as codex:
                account = codex.account()
                models = codex.models()
                rows = getattr(models, "data", models)
                model_count = len(rows) if isinstance(rows, list) else 0
                return {
                    "ok": True,
                    "enabled": True,
                    "authenticated": bool(account),
                    "sdk_version": str(version),
                    "model_count": model_count,
                    "latency_ms": round((time.monotonic() - started) * 1000),
                }
        except CodexProviderError:
            raise
        except Exception as exc:
            raise CodexProviderError(
                "Codex account health check failed.",
                failure_kind="auth_required" if "auth" in str(exc).lower() or "login" in str(exc).lower() else "unavailable",
                hint="Authenticate the protected Codex runtime or keep this route disabled.",
            ) from exc

    @classmethod
    def describe_image(
        cls, *, settings: dict[str, Any], prompt: str, image_bytes: bytes, mime: str,
        json_schema: dict[str, Any] | None,
    ) -> tuple[str, dict[str, Any]]:
        """One picture plus a question, answered as JSON (Codex ``exec -i``)."""
        if not is_enabled(settings):
            raise CodexProviderError("Codex provider is disabled.", failure_kind="disabled")
        if not _bridge_enabled(settings):
            raise CodexProviderError("Codex picture reading requires the protected runner bridge.", failure_kind="configuration")
        mime = "image/jpeg" if mime == "image/jpg" else mime
        if mime not in {"image/png", "image/jpeg", "image/webp"}:
            raise CodexProviderError(f"Codex cannot read a {mime} picture.", failure_kind="invalid_request")
        schema = json_schema if isinstance(json_schema, dict) else {
            "type": "object", "properties": {"answer": {"type": "string"}},
            "required": ["answer"], "additionalProperties": False,
        }
        return _bridge_generate(
            settings,
            system_prompt="You look at the attached picture and answer only with the requested JSON.",
            user_prompt=prompt,
            json_schema=schema,
            images=[{"mime": mime, "data_base64": base64.b64encode(image_bytes).decode("ascii")}],
        )

    @classmethod
    def list_models(cls, settings: dict[str, Any]) -> list[dict[str, str]]:
        if not is_enabled(settings):
            raise CodexProviderError("Codex provider is disabled.", failure_kind="disabled")
        if _bridge_enabled(settings):
            # The models the logged-in ChatGPT account offers, with their
            # reasoning efforts, read from the runner's Codex model cache.
            listed = _bridge_request(settings, "/v1/codex/models", timeout=15)
            models = []
            for row in listed.get("models") or []:
                if not isinstance(row, dict):
                    continue
                model_id = str(row.get("id") or "").strip()
                if not _MODEL_RE.fullmatch(model_id):
                    continue
                efforts = [e for e in (str(x).lower() for x in row.get("efforts") or []) if e in _ALLOWED_REASONING_EFFORTS]
                models.append({
                    "id": model_id,
                    "name": str(row.get("name") or model_id),
                    "efforts": efforts,
                    "default_effort": str(row.get("default_effort") or ""),
                })
            return models or [{"id": CODEX_MODEL, "name": "Codex (default)", "efforts": [], "default_effort": ""}]
        try:
            Codex, _, _, _, _ = _load_sdk()
            with Codex(_config(settings, reasoning=CODEX_REASONING_EFFORT)) as codex:
                payload = codex.models()
            rows = getattr(payload, "data", payload)
            models: list[dict[str, str]] = []
            for row in rows if isinstance(rows, list) else []:
                model_id = str(getattr(row, "id", "") or (row.get("id") if isinstance(row, dict) else "")).strip()
                name = str(getattr(row, "name", "") or (row.get("name") if isinstance(row, dict) else model_id)).strip()
                if model_id:
                    models.append({"id": model_id, "name": name or model_id})
            return models[:250]
        except CodexProviderError:
            raise
        except Exception as exc:
            raise CodexProviderError("Codex model discovery failed.", failure_kind="unavailable") from exc

    @classmethod
    def generate_structured(
        cls,
        *,
        settings: dict[str, Any],
        system_prompt: str,
        user_prompt: str,
        json_schema: dict[str, Any] | None,
        max_tokens: int,
        reasoning: str | None = None,
        cwd: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        if not is_enabled(settings):
            raise CodexProviderError("Codex provider is disabled.", failure_kind="disabled")
        if _bridge_enabled(settings):
            return _bridge_generate(
                settings,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                json_schema=json_schema,
            )
        started = time.monotonic()
        model = _runner_model(settings)
        effort = _runner_effort(settings)
        sandbox_name = _setting(settings, "codex_sandbox", "LYNEWRIGHT_CODEX_DEFAULT_SANDBOX", "read_only")
        if sandbox_name not in {"read_only", "workspace_write"}:
            sandbox_name = "read_only"
        try:
            Codex, _, ApprovalMode, Sandbox, _ = _load_sdk()
            with Codex(_config(settings, reasoning=effort)) as codex:
                thread = codex.thread_start(
                    approval_mode=ApprovalMode.deny_all,
                    cwd=cwd or _setting(settings, "codex_cwd", "LYNEWRIGHT_CODEX_CWD") or None,
                    model=model,
                    sandbox=getattr(Sandbox, sandbox_name),
                    ephemeral=True,
                )
                prompt = f"SYSTEM:\n{system_prompt}\n\nUSER:\n{user_prompt}"
                result = thread.run(
                    prompt,
                    approval_mode=ApprovalMode.deny_all,
                    effort=effort,
                    model=model,
                    output_schema=json_schema,
                    sandbox=getattr(Sandbox, sandbox_name),
                )
            text = str(getattr(result, "final_response", "") or "").strip()
            if not text:
                raise CodexProviderError("Codex returned an empty response.", failure_kind="empty_response")
            # The Lynewright parser remains authoritative; this early check only
            # gives callers a classified provider failure instead of prose.
            if json_schema is not None:
                json.loads(text)
            usage = _usage(result)
            usage["latency_ms"] = round((time.monotonic() - started) * 1000)
            usage["model"] = model
            usage["reasoning_effort"] = effort
            usage["sandbox"] = sandbox_name
            return text, usage
        except CodexProviderError:
            raise
        except json.JSONDecodeError as exc:
            raise CodexProviderError("Codex returned non-JSON content.", failure_kind="invalid_output") from exc
        except Exception as exc:
            message = str(exc)
            kind = "timeout" if "timeout" in message.lower() else "unavailable"
            raise CodexProviderError("Codex structured generation failed.", failure_kind=kind) from exc

    @classmethod
    def generate_image(
        cls,
        *,
        settings: dict[str, Any],
        prompt: str,
        negative_prompt: str = "",
        style_notes: str = "",
        size: str = "",
        count: int = 1,
    ) -> tuple[list[bytes], dict[str, Any]]:
        """Generate raster images through the protected built-in image_gen route."""
        if not is_enabled(settings):
            raise CodexProviderError("Codex provider is disabled.", failure_kind="disabled")
        if not _bridge_enabled(settings):
            raise CodexProviderError(
                "Built-in Codex image generation requires the protected runner bridge.",
                failure_kind="configuration",
            )
        images: list[bytes] = []
        last_usage: dict[str, Any] = {}
        for index in range(max(1, min(int(count), 4))):
            item_prompt = prompt if index == 0 else f"{prompt}\nCreate a distinct variation {index + 1}."
            content, usage = _bridge_generate_image(
                settings,
                prompt=item_prompt,
                negative_prompt=negative_prompt,
                style_notes=style_notes,
                size=size,
            )
            images.append(content)
            last_usage = usage
        last_usage["count"] = len(images)
        return images, last_usage

    @classmethod
    def submit_image(
        cls, *, settings: dict[str, Any], prompt: str, negative_prompt: str = "",
        style_notes: str = "", size: str = "", timeout_seconds: int = 600,
    ) -> dict[str, Any]:
        """Submit an idempotent protected image job without waiting for it."""
        if not is_enabled(settings):
            raise CodexProviderError("Codex provider is disabled.", failure_kind="disabled")
        if not _bridge_enabled(settings):
            raise CodexProviderError("Built-in Codex image generation requires the protected runner bridge.", failure_kind="configuration")
        return _bridge_submit_image(
            settings, prompt=prompt, negative_prompt=negative_prompt, style_notes=style_notes,
            size=size, timeout_seconds=timeout_seconds,
        )

    @classmethod
    def poll_image(cls, *, settings: dict[str, Any], job_id: str) -> dict[str, Any]:
        """Read one runner state and fetch the artifact only when it is ready."""
        if not is_enabled(settings) or not _bridge_enabled(settings):
            raise CodexProviderError("The protected Codex image runner is unavailable.", failure_kind="configuration")
        return _bridge_poll_image(settings, job_id)

    @classmethod
    def cancel_image(cls, *, settings: dict[str, Any], job_id: str) -> None:
        if not is_enabled(settings) or not _bridge_enabled(settings):
            return
        _bridge_cancel_image(settings, job_id)
