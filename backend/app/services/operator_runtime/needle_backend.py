"""Deterministic-safe Needle 2 candidate selection for the Operator loop.

Needle is deliberately kept on the proposal side of the boundary. It gets
raw JSON schemas for the exact authorised operation snapshot and returns one
candidate call. Lynewright still owns schema validation, evidence grounding,
permissions, risk policy, approvals, idempotency, execution, and the cloud
fallback.

The adapter uses the official ``cactus-needle`` API. In particular, it calls
``Needle.complete`` and never ``Needle.run``: ``run`` would execute Python
functions supplied to Needle and would create a second tool runtime.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from app.services.ai_provider import AiProviderError
from app.services.operator_runtime.decisions import validate_decision


NEEDLE_PACKAGE = "cactus-needle"
NEEDLE_GENERATION = "2"
NEEDLE_MODEL_PREFIX = f"needle{NEEDLE_GENERATION}@"
DEFAULT_READ_CONFIDENCE = 0.92
DEFAULT_WRITE_CONFIDENCE = 0.97
DEFAULT_SENSITIVE_CONFIDENCE = 0.99
NEEDLE_POLICIES = frozenset({"cloud_only", "needle_shadow", "needle_assisted", "needle_only"})
SENSITIVE_RISK_CLASSES = frozenset({
    "PUBLICATION", "MONEY_MOVING", "DESTRUCTIVE", "SECURITY_SENSITIVE",
})
WRITE_RISK_CLASSES = frozenset({"SAFE_OPERATION", "BOUNDED_CONFIG"})
DETERMINISTIC_ROOT_ARGUMENTS = frozenset({"project_id", "tenant_id", "idempotency_key"})
NEEDLE_SYSTEM_PROMPT = (
    "Choose one declared operation that best matches the request. Return one "
    "function call with values explicit in the request or trusted evidence. "
    "For an unmatched request, return an empty function-call list."
)

# These values are never allowed to be authored by a local candidate. The
# reducer may inject trusted values after the local decision or the cloud
# verifier may resolve them from a validated requirement/evidence packet.
PRIVILEGED_ARGUMENT_NAMES = frozenset({
    "tenant", "tenant_id", "workspace_id", "project", "project_id",
    "credential", "credential_id", "credential_ref", "secret", "token",
    "resource_id", "integration_id", "media_id", "job_id", "execution_id",
    "account_id", "publisher_id", "publisher_account_id", "channel_id",
    "destructive_target", "confirm_project_id", "confirm_media_id",
    "confirm_name", "idempotency_key",
})
_ID_NAME_RE = re.compile(r"(?:^|_)(?:id|uuid|key)$", re.IGNORECASE)

_AGENT_CACHE: dict[str, Any] = {}
_AGENT_CACHE_LOCK = threading.RLock()


def _json_fingerprint(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def operation_schema_fingerprint(operations: list[dict[str, Any]] | None) -> str:
    """Fingerprint the complete authorised operation list."""
    normalized: list[dict[str, Any]] = []
    for item in operations or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        normalized.append({
            "name": str(item.get("name")),
            "description": str(item.get("description") or ""),
            "input_schema": item.get("input_schema") if isinstance(item.get("input_schema"), dict) else {},
            "risk_class": str(item.get("risk_class") or "SAFE_OPERATION"),
            "capability": str(item.get("capability") or ""),
            "read_only": bool(item.get("read_only")),
        })
    normalized.sort(key=lambda item: item["name"])
    return _json_fingerprint(normalized)


def installed_needle_version() -> str:
    try:
        return importlib.metadata.version(NEEDLE_PACKAGE)
    except importlib.metadata.PackageNotFoundError:
        return "uninstalled"
    except Exception:
        return "unknown"


def _engine_path() -> Path | None:
    configured = (
        os.getenv("LYNEWRIGHT_OPERATOR_NEEDLE_ENGINE_PATH")
        or os.getenv("NEEDLE2_LIB_PATH")
        or os.getenv("NEEDLE_LIB_PATH")
    )
    if not configured:
        return None
    path = Path(configured)
    return path if path.is_file() else None


def needle_engine_checksum() -> str:
    configured = str(os.getenv("LYNEWRIGHT_OPERATOR_NEEDLE_ENGINE_SHA256") or "").strip().lower()
    if configured and not re.fullmatch(r"[0-9a-f]{64}", configured):
        return "unknown"
    manifest_expected = ""
    manifest = str(os.getenv("LYNEWRIGHT_OPERATOR_NEEDLE_ENGINE_CHECKSUM_FILE") or "").strip()
    if manifest:
        try:
            line = Path(manifest).read_text(encoding="utf-8").splitlines()[0]
            candidate = line.split()[0].strip().lower()
            if re.fullmatch(r"[0-9a-f]{64}", candidate):
                manifest_expected = candidate
        except (OSError, IndexError):
            pass
    path = _engine_path()
    if path is not None:
        try:
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            actual = digest.hexdigest()
            expected = configured or manifest_expected
            if expected and actual != expected:
                return "unknown"
            return actual
        except OSError:
            pass
    return "unknown"


def needle_model_id() -> str:
    return f"{NEEDLE_MODEL_PREFIX}{needle_engine_checksum()}"


def configured_needle_mode() -> str:
    value = os.getenv("LYNEWRIGHT_OPERATOR_NEEDLE_MODE", "off").strip().lower()
    return value if value in {"off", "shadow", "canary"} else "off"


def _threshold_for(risk_class: str, settings: dict[str, Any], *, read_only: bool = False) -> float:
    if risk_class in SENSITIVE_RISK_CLASSES:
        key, default = "needle_sensitive_confidence", DEFAULT_SENSITIVE_CONFIDENCE
    elif not read_only and risk_class in WRITE_RISK_CLASSES:
        key, default = "needle_write_confidence", DEFAULT_WRITE_CONFIDENCE
    else:
        key, default = "needle_read_confidence", DEFAULT_READ_CONFIDENCE
    try:
        value = float(settings.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(0.0, min(1.0, value))


def _bounded_tokens(value: Any) -> int:
    try:
        return max(64, min(2048, int(value or 1024)))
    except (TypeError, ValueError):
        return 1024


def _authorised_operations(payload: dict[str, Any]) -> list[dict[str, Any]]:
    source = payload.get("authorized_operations")
    if not isinstance(source, list):
        source = payload.get("tools")
    operations: list[dict[str, Any]] = []
    for item in source or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        # A backend caller may pass the full operation_list and an ``authorized`` bit;
        # never allow a false entry to reach the local model.
        if item.get("authorized") is False:
            continue
        operations.append(dict(item))
    operations.sort(key=lambda item: str(item.get("name") or ""))
    return operations


def _judge_operation_subset(
    operations: list[dict[str, Any]], payload: dict[str, Any], *, limit: int = 8,
) -> list[dict[str, Any]]:
    """Select a bounded, deterministic operation packet for the cloud judge."""
    if len(operations) <= limit:
        return operations
    objective = str(payload.get("objective") or "").casefold()
    tokens = {token for token in re.findall(r"[\w-]{3,}", objective) if token}

    def score(item: dict[str, Any]) -> tuple[int, str]:
        haystack = " ".join((
            str(item.get("name") or ""),
            str(item.get("description") or ""),
            str(item.get("capability") or ""),
        )).casefold()
        overlap = sum(1 for token in tokens if token in haystack)
        # Keep deterministic ordering for ties so the same schema fingerprint
        # always yields the same compact judge context.
        return (-overlap, str(item.get("name") or ""))

    ranked = sorted(operations, key=score)
    return ranked[: max(1, int(limit))]


def _needle_tools(operations: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, dict[str, Any]]]:
    """Build raw JSON function schemas with stable, Needle-safe aliases."""
    tools: list[dict[str, Any]] = []
    aliases: dict[str, str] = {}
    by_alias: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(operations, start=1):
        canonical = str(item.get("name") or "")
        slug = re.sub(r"[^A-Za-z0-9]+", "_", canonical).strip("_").lower()[:48]
        alias = f"op_{index:03d}{('_' + slug) if slug else ''}"
        legacy_alias = f"op_{index:03d}"
        schema = item.get("input_schema")
        if not isinstance(schema, dict) or not schema:
            schema = {"type": "object", "properties": {}, "additionalProperties": True}
        tools.append({
            "name": alias,
            "description": str(item.get("description") or canonical)[:2000],
            "parameters": schema,
        })
        aliases[alias] = canonical
        by_alias[alias] = item
        # Keep compatibility with qualification fixtures that return the old
        # numeric-only alias; the actual schema exposed to Needle retains the
        # semantic operation name so the local model can select it reliably.
        aliases[legacy_alias] = canonical
        by_alias[legacy_alias] = item
    return tools, aliases, by_alias


def _validate_operation_schemas(operations: list[dict[str, Any]]) -> None:
    """Reject an unsupported operation list before asking the local model to use it."""
    for item in operations:
        schema = item.get("input_schema")
        if not isinstance(schema, dict) or not schema:
            continue
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise ValueError(f"needle_unsupported_schema:{item.get('name')}") from exc


def _candidate_schema_error(
    *, operation: dict[str, Any], arguments: dict[str, Any], canonical_name: str,
) -> str:
    """Validate a candidate while allowing only mapped root defaults to be injected later."""
    schema = operation.get("input_schema")
    if not isinstance(schema, dict) or not schema:
        return ""
    candidate_schema = schema
    # Mapped operations have a trusted reducer that fills these root fields
    # after selection. Needle must not author them, but their absence should
    # not turn an otherwise valid local candidate into a paid fallback.
    if canonical_name.startswith("lynewright.integrations.mapped.") or bool(operation.get("read_only")):
        candidate_schema = deepcopy(schema)
        required = candidate_schema.get("required")
        if isinstance(required, list):
            candidate_schema["required"] = [
                name for name in required
                if name not in DETERMINISTIC_ROOT_ARGUMENTS
            ]
    try:
        errors = sorted(
            Draft202012Validator(candidate_schema).iter_errors(arguments),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
    except SchemaError as exc:
        return f"needle_unsupported_schema:{canonical_name}"
    if errors:
        path = ".".join(str(part) for part in errors[0].absolute_path)
        return f"needle_schema_invalid:{path or 'arguments'}:{errors[0].validator}"
    return ""


def _cache_index_path(fingerprint: str) -> str | None:
    root_value = str(os.getenv("LYNEWRIGHT_OPERATOR_NEEDLE_CACHE_DIR") or "").strip()
    if not root_value:
        return None
    root = Path(root_value)
    try:
        root.mkdir(parents=True, exist_ok=True)
        return str(root / f"tools-{fingerprint}.idx")
    except OSError as exc:
        raise AiProviderError(
            "Needle retrieval cache is unavailable.",
            failure_kind="decision_backend_unavailable",
        ) from exc


def _build_agent(tools: list[dict[str, Any]], fingerprint: str) -> Any:
    try:
        import needle  # type: ignore
    except ImportError as exc:  # pragma: no cover - deployment opt-in
        raise AiProviderError(
            "Needle is not installed for the local decision canary.",
            failure_kind="decision_backend_unavailable",
        ) from exc
    constructor = getattr(needle, "Needle", None)
    if not callable(constructor):
        raise AiProviderError(
            "The installed Needle package does not expose Needle(...).",
            failure_kind="decision_backend_unavailable",
        )
    kwargs: dict[str, Any] = {"tools": tools, "system": NEEDLE_SYSTEM_PROMPT}
    index_path = _cache_index_path(fingerprint)
    if index_path:
        kwargs["tool_index_path"] = index_path
    try:
        return constructor(**kwargs)
    except Exception as exc:  # pragma: no cover - depends on native engine
        raise AiProviderError(
            f"Needle engine initialization failed: {str(exc)[:300]}",
            failure_kind="decision_backend_unavailable",
        ) from exc


def _agent_for(tools: list[dict[str, Any]], fingerprint: str) -> Any:
    # The key is a schema fingerprint, so a cached instance can never carry an
    # operation set from another tenant/profile into this decision.
    with _AGENT_CACHE_LOCK:
        agent = _AGENT_CACHE.get(fingerprint)
        if agent is None:
            agent = _build_agent(tools, fingerprint)
            _AGENT_CACHE[fingerprint] = agent
        return agent


def _needle_complete(agent: Any, prompt: str, *, max_tokens: int) -> Any:
    """Call the official one-turn API; deliberately no ``run`` fallback."""
    reset = getattr(agent, "reset", None)
    if not callable(reset):
        raise AiProviderError(
            "The installed Needle agent does not expose reset().",
            failure_kind="decision_backend_unavailable",
        )
    reset()
    complete = getattr(agent, "complete", None)
    if not callable(complete):
        raise AiProviderError(
            "The installed Needle agent does not expose complete().",
            failure_kind="decision_backend_unavailable",
        )
    try:
        return complete(prompt, max_new_tokens=_bounded_tokens(max_tokens))
    except Exception as exc:  # pragma: no cover - depends on native engine
        raise AiProviderError(
            f"Needle local completion failed: {str(exc)[:300]}",
            failure_kind="decision_backend_error",
        ) from exc


def _decode_result(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "output"):
        value = getattr(value, "output")
    if hasattr(value, "text"):
        value = getattr(value, "text")
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if not isinstance(value, str):
        raise ValueError("Needle returned neither a response object nor text.")
    text = value.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].removesuffix("```").strip()
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("Needle returned JSON that is not an object.")
    return parsed


def _grounding_corpus(payload: dict[str, Any]) -> str:
    values = {
        "objective": payload.get("objective"),
        "task_memory": payload.get("task_memory"),
        "verified_evidence": payload.get("verified_evidence"),
        "recent_steps": payload.get("recent_steps"),
        "conversation_history": payload.get("conversation_history"),
        "wake_observation": payload.get("wake_observation"),
    }
    return json.dumps(values, ensure_ascii=False, default=str).casefold()


def _needle_prompt(payload: dict[str, Any]) -> str:
    """Build a small, unambiguous local prompt from the run snapshot.

    Needle is a candidate selector, not a transcript model. Keeping only the
    objective, bounded facts, and a few recent steps both reduces local work
    and avoids labels such as ``trusted`` being mistaken for user arguments.
    """
    objective = str(payload.get("objective") or "").strip()[:2000]

    def compact(value: Any, limit: int) -> str:
        encoded = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
        return encoded[:limit]

    lines = [f"Task: {objective}"]
    evidence = payload.get("verified_evidence")
    if evidence:
        lines.append(f"Facts: {compact(evidence, 2400)}")
    memory = payload.get("task_memory")
    if memory:
        lines.append(f"Memory: {compact(memory, 1600)}")
    recent = payload.get("recent_steps")
    if isinstance(recent, list) and recent:
        lines.append(f"Recent: {compact(recent[-4:], 2400)}")
    lines.append("Choose the best declared operation.")
    return "\n".join(lines)


def validate_candidate_grounding(arguments: dict[str, Any], payload: dict[str, Any]) -> str:
    """Fail closed when Needle authors a privileged or unevidenced field."""
    corpus = _grounding_corpus(payload)
    for key, value in arguments.items():
        normalized = str(key or "").strip().casefold()
        if normalized in PRIVILEGED_ARGUMENT_NAMES or _ID_NAME_RE.search(normalized):
            return f"local_candidate_privileged_argument:{normalized}"
        if value in (None, "", [], {}):
            continue
        if isinstance(value, bool):
            token = "true" if value else "false"
        else:
            token = str(value).casefold()
        if isinstance(value, (dict, list)):
            leaves = _scalar_leaves(value)
            if any(str(leaf).casefold() not in corpus for leaf in leaves if str(leaf)):
                return f"local_candidate_ungrounded_argument:{normalized}"
        elif token not in corpus:
            return f"local_candidate_ungrounded_argument:{normalized}"
    return ""


def _candidate_is_repeat(*, canonical: str, arguments: dict[str, Any], payload: dict[str, Any]) -> bool:
    """Detect an unchanged read before it reaches the tool runtime."""
    recent = payload.get("recent_steps")
    if not isinstance(recent, list):
        return False
    candidate = _json_fingerprint({"tool": canonical, "arguments": arguments})
    for step in recent:
        if not isinstance(step, dict) or str(step.get("tool") or "") != canonical:
            continue
        if str(step.get("status") or "") not in {"succeeded", "failed"}:
            continue
        previous = _json_fingerprint({
            "tool": canonical,
            "arguments": step.get("arguments") if isinstance(step.get("arguments"), dict) else {},
        })
        if previous == candidate:
            return True
    return False


def _scalar_leaves(value: Any) -> list[Any]:
    if isinstance(value, dict):
        result: list[Any] = []
        for child in value.values():
            result.extend(_scalar_leaves(child))
        return result
    if isinstance(value, list):
        result: list[Any] = []
        for child in value:
            result.extend(_scalar_leaves(child))
        return result
    return [value]


def _merge_attempt_record(settings: dict[str, Any], record: dict[str, Any]) -> None:
    existing = settings.get("_last_provider_attempt_records")
    records = list(existing) if isinstance(existing, list) else []
    records.insert(0, record)
    settings["_last_provider_attempt_records"] = records[:16]


class NeedleDecisionBackend:
    """Needle proposal adapter with one bounded fallback decision."""

    name = "needle"

    def __init__(
        self, *, fallback: Any, system_prompt: str, json_schema: dict[str, Any],
        mode: str = "canary", sensitive_fallback: Any | None = None,
        judge_fallback: Any | None = None,
    ) -> None:
        self.fallback = fallback
        self.sensitive_fallback = sensitive_fallback or fallback
        self.judge_fallback = judge_fallback or self.sensitive_fallback
        self.system_prompt = system_prompt
        self.json_schema = json_schema
        self.mode = mode if mode in {"shadow", "canary"} else "canary"

    def _fallback(
        self, *, settings: dict[str, Any], payload: dict[str, Any], reason: str,
        local_record: dict[str, Any] | None = None, sensitive: bool = False,
    ) -> dict[str, Any]:
        settings["_needle_status"] = "fallback"
        settings["_needle_failure"] = reason[:240]
        settings["_fallback_reason"] = reason[:240]
        if sensitive:
            settings["_sensitive_verification_required"] = True
        local_records = [
            dict(item) for item in (settings.get("_last_provider_attempt_records") or [])
            if isinstance(item, dict) and item.get("provider") == "local_needle"
        ]
        for item in local_records:
            item["outcome"] = "fallback"
            if sensitive:
                item["failure_kind"] = "sensitive_verification_required"
        if local_record:
            local_record["outcome"] = "fallback"
            local_records.insert(0, local_record)
        # Publish the local attempt before entering the cloud provider. If the
        # fallback itself fails, the durable loop still gets an honest local
        # attempt record instead of losing the reason Needle was bypassed.
        settings["_last_provider_attempt_records"] = local_records[:16]
        # Shadow compares the local proposal with the ordinary operator route.
        # Control-mode fallbacks always use the separately configured judge so
        # a local failure cannot silently return to the small/ordinary model.
        backend = self.sensitive_fallback if sensitive else (
            self.fallback if self.mode == "shadow" else self.judge_fallback
        )
        cloud_payload = dict(payload)
        if backend is self.judge_fallback or sensitive:
            operations = _authorised_operations(payload)
            cloud_payload["judge_operations"] = _judge_operation_subset(operations, payload)
            cloud_payload.pop("authorized_operations", None)
            cloud_payload["tools"] = cloud_payload["judge_operations"]
            cloud_payload["judge_context"] = {
                "fallback_reason": reason[:240],
                "candidate": settings.get("_needle_candidate") or {},
                "decision_fingerprint": str(settings.get("_decision_fingerprint") or ""),
                "repeat_detected": bool(settings.get("_repeat_detected")),
            }
        if sensitive and settings.get("_needle_candidate"):
            cloud_payload["verification_request"] = {
                "candidate": settings["_needle_candidate"],
                "instruction": "Verify this candidate against the objective and evidence; reject it if unsupported.",
            }
        try:
            result = backend.decide(settings=settings, payload=cloud_payload)
            if backend is self.judge_fallback or sensitive:
                settings["_decision_backend"] = "operator_judge"
            return result
        finally:
            cloud_records = [
                dict(item) for item in (settings.get("_last_provider_attempt_records") or [])
                if isinstance(item, dict) and item.get("provider") != "local_needle"
            ]
            settings["_last_provider_attempt_records"] = (local_records + cloud_records)[:16]

    def decide(self, *, settings: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        policy = str(settings.get("_operator_decision_policy") or "").strip().lower()
        ceiling = configured_needle_mode()
        explicit_backend = str(settings.get("_needle_explicit_backend") or "") == "1"
        if policy and policy == "cloud_only" and not explicit_backend and ceiling != "shadow":
            settings["_needle_status"] = "disabled_by_profile"
            settings["_fallback_reason"] = "profile_cloud_only"
            return self.fallback.decide(settings=settings, payload=payload)
        # A directly constructed adapter (used by qualification tests) has no
        # profile snapshot. Production calls always carry one, so the global
        # emergency ceiling still disables every profile without ambiguity.
        if policy and ceiling == "off" and not explicit_backend:
            settings["_needle_status"] = "disabled_by_global_mode"
            settings["_fallback_reason"] = "global_mode_off"
            return self.fallback.decide(settings=settings, payload=payload)

        local_only = policy == "needle_only"
        active_mode = "shadow" if ceiling == "shadow" or policy == "needle_shadow" else self.mode
        if policy == "needle_assisted" and ceiling == "canary":
            try:
                canary_percent = max(0, min(100, int(settings.get("_needle_canary_percent") or 0)))
            except (TypeError, ValueError):
                canary_percent = 0
            if canary_percent < 100:
                run_id = str(settings.get("_operator_run_id") or payload.get("run_id") or "")
                bucket = int(hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:8], 16) % 100 if run_id else 100
                if bucket >= canary_percent:
                    settings["_needle_status"] = "outside_canary"
                    settings["_fallback_reason"] = f"profile_canary_percent:{canary_percent}"
                    return self.fallback.decide(settings=settings, payload=payload)
        operations = _authorised_operations(payload)
        tools, aliases, by_alias = _needle_tools(operations)
        fingerprint = operation_schema_fingerprint(operations)
        started = time.monotonic()
        local_record: dict[str, Any] = {
            "attempt_id": f"needle_{hashlib.sha256(f'{fingerprint}|{time.time_ns()}'.encode()).hexdigest()[:24]}",
            "provider": "local_needle",
            "protocol": "needle_complete",
            "model": needle_model_id(),
            "network_attempt": 0,
            "outcome": "rejected",
            "failure_kind": "",
            "usage": {"prompt_chars": 0, "exact": 0},
        }
        try:
            _validate_operation_schemas(operations)
            if not tools:
                raise ValueError("authorized_operation_operation_list_empty")
            snapshot_fingerprint = str(settings.get("_tool_schema_fingerprint") or "").strip()
            if snapshot_fingerprint and snapshot_fingerprint != fingerprint:
                raise ValueError("needle_operation list_changed_after_run_snapshot")
            settings["_needle_schema_fingerprint"] = fingerprint
            settings["_needle_package_version"] = installed_needle_version()
            settings["_needle_model"] = needle_model_id()
            prompt = _needle_prompt(payload)
            local_record["usage"]["prompt_chars"] = len(prompt)
            agent = _agent_for(tools, fingerprint)
            response = _decode_result(_needle_complete(
                agent, prompt,
                max_tokens=settings.get("_operator_decision_max_tokens") or 1024,
            ))
            validation = response.get("validation")
            if isinstance(validation, dict) and bool(validation.get("negation")):
                raise ValueError("needle_negation_detected")
            response_type = response.get("type")
            if response_type is not None and response_type != "call":
                raise ValueError("needle_expected_call_response")
            calls = response.get("function_calls")
            if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
                raise ValueError("needle_expected_exactly_one_function_call")
            call = calls[0]
            alias = str(call.get("name") or "")
            canonical = aliases.get(alias)
            if not canonical or alias not in by_alias:
                raise ValueError("needle_operation_alias_not_authorized")
            arguments = call.get("arguments")
            if not isinstance(arguments, dict):
                raise ValueError("needle_arguments_must_be_object")
            try:
                confidence = float(response.get("confidence"))
            except (TypeError, ValueError):
                raise ValueError("needle_confidence_missing")
            if not 0.0 <= confidence <= 1.0:
                raise ValueError("needle_confidence_invalid")
            operation = by_alias[alias]
            risk_class = str(operation.get("risk_class") or "SAFE_OPERATION")
            threshold = _threshold_for(
                risk_class, settings, read_only=bool(operation.get("read_only")),
            )
            settings["_needle_confidence"] = confidence
            settings["_needle_risk_class"] = risk_class
            settings["_needle_threshold"] = threshold
            if confidence < threshold:
                raise ValueError(f"needle_confidence_below_threshold:{confidence:.4f}<{threshold:.4f}")
            schema_error = _candidate_schema_error(
                operation=operation, arguments=arguments, canonical_name=canonical,
            )
            if schema_error:
                raise ValueError(schema_error)
            grounding_error = validate_candidate_grounding(arguments, payload)
            if grounding_error:
                raise ValueError(grounding_error)
            settings["_decision_fingerprint"] = _json_fingerprint({
                "tool": canonical,
                "arguments": arguments,
                "evidence": payload.get("verified_evidence") or {},
            })
            if bool(operation.get("read_only")) and _candidate_is_repeat(
                canonical=canonical, arguments=arguments, payload=payload,
            ):
                settings["_repeat_detected"] = True
                raise ValueError("needle_repeated_candidate")
            settings["_evidence_validation_result"] = "passed"
            sensitive = risk_class in SENSITIVE_RISK_CLASSES
            local_record["outcome"] = "verified_required" if sensitive else "accepted"
            local_record["failure_kind"] = "sensitive_verification_required" if sensitive else ""
            settings["_needle_latency_ms"] = max(0, round((time.monotonic() - started) * 1000))
            settings["_sensitive_verification_required"] = sensitive
            settings["_needle_candidate"] = {
                "tool": canonical, "arguments": arguments,
                "confidence": confidence, "risk_class": risk_class,
            }
            _merge_attempt_record(settings, local_record)
            if active_mode == "shadow" or sensitive:
                if sensitive:
                    return self._fallback(
                        settings=settings, payload=payload,
                        reason="sensitive_verification_required", sensitive=True,
                    )
                settings["_needle_status"] = "shadow_valid"
                return self.fallback.decide(settings=settings, payload=payload)
            settings["_decision_backend"] = "local_needle"
            settings["_last_provider_used"] = "local_needle"
            settings["_last_provider_protocol"] = "needle_complete"
            settings["_last_provider_model"] = needle_model_id()
            settings["_last_provider_attempt_count"] = 1
            settings["_needle_status"] = "accepted"
            return validate_decision({
                "type": "tool_call",
                "tool": canonical,
                "arguments": arguments,
                "message": str(response.get("reasoning") or "")[:2000],
            })
        except Exception as exc:
            reason = str(exc)[:240]
            local_record["failure_kind"] = reason
            settings["_needle_latency_ms"] = max(0, round((time.monotonic() - started) * 1000))
            if local_only:
                settings["_needle_status"] = "rejected"
                settings["_fallback_reason"] = reason
                _merge_attempt_record(settings, local_record)
                raise AiProviderError(
                    f"Needle-only decision rejected: {reason}",
                    failure_kind="needle_rejected",
                ) from exc
            return self._fallback(
                settings=settings, payload=payload, reason=reason,
                local_record=local_record,
                sensitive=str(settings.get("_needle_risk_class") or "") in SENSITIVE_RISK_CLASSES,
            )


__all__ = [
    "DEFAULT_READ_CONFIDENCE",
    "DEFAULT_SENSITIVE_CONFIDENCE",
    "DEFAULT_WRITE_CONFIDENCE",
    "NeedleDecisionBackend",
    "configured_needle_mode",
    "installed_needle_version",
    "needle_engine_checksum",
    "needle_model_id",
    "operation_schema_fingerprint",
    "validate_candidate_grounding",
]
