"""Small, deterministic retrieval/context helpers for Operator decisions.

This module deliberately has no provider or business-domain dependency.  It
keeps the model packet small while retaining exact identifiers, current task
state, and the evidence needed to choose the next generic operation.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any, Callable, Iterable


_ARABIC_DIACRITICS = re.compile(r"[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed]")
_STOP_WORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "for", "from", "in", "is", "it",
    "my", "of", "on", "or", "the", "to", "use", "with", "this", "that",
    "please", "can", "you", "هل", "من", "في", "على", "إلى", "و", "أ", "أن",
    "هذا", "هذه", "هل", "لي", "ثم", "منشور", "رسالة",
})


def normalize_text(value: Any) -> str:
    """Normalize search text without changing the value shown to a user."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = _ARABIC_DIACRITICS.sub("", text).replace("ـ", "")
    return " ".join(text.split())


def search_terms(value: Any) -> tuple[str, ...]:
    """Return Unicode-aware terms with harmless request words removed."""
    terms = re.findall(r"[^\W_]+", normalize_text(value), flags=re.UNICODE)
    return tuple(term for term in terms if len(term) > 1 and term not in _STOP_WORDS)


def stable_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def estimate_tokens(value: Any) -> int:
    """Conservative multilingual estimate used for admission/packing only."""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    # Non-Latin text commonly uses more provider tokens per character.  This
    # is an estimate, never a replacement for provider-reported usage.
    non_ascii = sum(1 for char in text if ord(char) > 127)
    divisor = 3.0 if non_ascii > len(text) * 0.20 else 4.0
    return max(1, int(len(text) / divisor + 0.999))


def compact_history(
    value: Any, *, query: str = "", limit: int = 6, max_chars: int = 2400,
) -> list[dict[str, str]]:
    """Keep recent relevant turns, retaining the newest turns first."""
    if not isinstance(value, list):
        return []
    query_terms = set(search_terms(query))
    candidates: list[tuple[int, int, dict[str, str]]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        excerpt = text[:700]
        overlap = len(query_terms.intersection(search_terms(excerpt)))
        # Newer turns win ties; relevant older corrections remain eligible.
        candidates.append((overlap, index, {
            "role": "assistant" if str(item.get("role") or "") == "assistant" else "user",
            "text": excerpt,
        }))
    selected: list[tuple[int, dict[str, str]]] = []
    total = 0
    for overlap, index, item in sorted(candidates, key=lambda row: (row[0], row[1]), reverse=True):
        if total + len(item["text"]) > max_chars:
            continue
        selected.append((index, item))
        total += len(item["text"])
        if len(selected) >= limit:
            break
    return [item for _, item in sorted(selected, key=lambda row: row[0])]


def project_result(value: Any, *, max_chars: int = 900, max_items: int = 8) -> Any:
    """Project a tool result into a useful packet while preserving refs/status."""
    if isinstance(value, list):
        if not value:
            return []
        # Lists are common for tool directory/media/resource reads.  Previously we
        # bounded the number of items but not the encoded size of the list,
        # so one read could be copied into every following model prompt.  Keep
        # the leading candidates (list operations are normally newest-first),
        # preserve their identifiers, and expose a compact envelope when the
        # complete preview would exceed the budget.
        candidates: list[Any] = []
        item_budget = max(96, min(max_chars, max(160, max_chars // max(1, min(max_items, len(value))))))
        for item in value[:max_items]:
            candidate = project_result(item, max_chars=item_budget, max_items=max_items)
            tentative = candidates + [candidate]
            if len(json.dumps(tentative, ensure_ascii=False, default=str, separators=(",", ":"))) > max_chars:
                break
            candidates.append(candidate)
        if not candidates:
            candidates.append(project_result(value[0], max_chars=max_chars, max_items=max_items))
        if len(candidates) == len(value):
            return candidates
        envelope = {
            "items": candidates,
            "total": len(value),
            "truncated": True,
            "digest": stable_digest(value),
        }
        encoded = json.dumps(envelope, ensure_ascii=False, default=str, separators=(",", ":"))
        if len(encoded) <= max_chars:
            return envelope
        # The first candidate is the most useful safe fallback when even the
        # envelope cannot fit the caller's hard limit.
        return {"items": candidates[:1], "total": len(value), "truncated": True}
    if not isinstance(value, dict):
        text = str(value or "")
        return text[:max_chars] + ("…" if len(text) > max_chars else "")
    important = {
        "id", "*_id", "operation_id", "execution_id", "job_id", "media_id", "artifact_id",
        "status", "state", "terminal", "success", "error", "message", "name", "title",
        "url", "content_type", "created_at", "updated_at", "project_id", "required", "missing", "next", "result_ref", "source_ref",
        "revision", "count", "total", "items", "products", "operations", "async_operation",
    }
    projected: dict[str, Any] = {}
    for key, child in value.items():
        key_text = str(key)
        if key_text in important or key_text.endswith("_id") or key_text.endswith("_ref"):
            projected[key_text] = project_result(child, max_chars=max_chars // 2, max_items=max_items)
    if not projected:
        for key, child in list(value.items())[:max_items]:
            projected[str(key)] = project_result(child, max_chars=max_chars // 3, max_items=max_items)
    encoded = json.dumps(projected, ensure_ascii=False, default=str, separators=(",", ":"))
    if len(encoded) <= max_chars:
        return projected
    # Keep identity/status fields even when a collection is too large.  The
    # model can request the full result by its typed ref if it needs detail.
    essential = {
        key: child for key, child in projected.items()
        if key in {"id", "operation_id", "execution_id", "job_id", "media_id", "artifact_id", "status", "state", "terminal", "success", "revision"}
    }
    envelope = {"truncated": True, "digest": stable_digest(value), **essential}
    prefix = json.dumps(envelope, ensure_ascii=False, default=str, separators=(",", ":"))
    remaining = max(0, max_chars - len(prefix) - 16)
    if remaining:
        envelope["preview"] = encoded[:remaining]
    return envelope


def rank_tools(
    tools: Iterable[dict[str, Any]], *, query: str, limit: int = 8,
    semantic_score: Callable[[str, str], float] | None = None,
) -> list[dict[str, Any]]:
    """Rank authorized tool cards using lexical overlap and optional semantics."""
    query_terms = set(search_terms(query))
    ranked: list[tuple[float, int, dict[str, Any]]] = []
    for index, item in enumerate(tools):
        name = str(item.get("name") or "")
        searchable = " ".join(str(item.get(key) or "") for key in ("name", "description", "capability", "keywords", "contract", "operation"))
        terms = set(search_terms(searchable))
        exact = 3.0 if normalize_text(query) and normalize_text(query) in normalize_text(searchable) else 0.0
        lexical = float(len(query_terms.intersection(terms)))
        semantic = float(semantic_score(query, searchable) or 0.0) if semantic_score else 0.0
        # Risky operations are not preferred unless the objective names a
        # mutation intent. Authorization remains enforced by the runtime.
        score = exact + lexical + semantic
        if score:
            ranked.append((score, -index, item))
    ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [item for _, _, item in ranked[:limit]]


def task_memory(
    *, objective: str, context: dict[str, Any], recent_steps: Iterable[dict[str, Any]],
    max_chars: int = 2400, read_tools: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    """Build a deterministic checkpoint from verified run state and steps."""
    memory = context.get("task_memory") if isinstance(context.get("task_memory"), dict) else {}
    result: dict[str, Any] = {
        "version": 2,
        "goal": str(memory.get("goal") or objective)[:800],
        "stage": str(memory.get("stage") or "")[:80],
        "entities": list(memory.get("entities") or [])[:12],
        "selected_operation": memory.get("selected_operation"),
        "completed_stages": list(memory.get("completed_stages") or [])[-8:],
        "pending_execution_ref": memory.get("pending_execution_ref"),
        "artifacts": list(memory.get("artifacts") or [])[-8:],
        "last_read": memory.get("last_read"),
        "last_blocker": str(memory.get("last_blocker") or "")[:400],
    }
    readable = set(read_tools or ())
    completed = {
        (str(item.get("tool") or ""), item.get("sequence"))
        for item in result["completed_stages"] if isinstance(item, dict)
    }
    artifacts = {
        (str(item.get("kind") or ""), str(item.get("value") or ""))
        for item in result["artifacts"] if isinstance(item, dict)
    }
    for step in recent_steps:
        if not isinstance(step, dict):
            continue
        tool = str(step.get("tool_name") or step.get("tool") or "")
        raw_output = step.get("output")
        output = raw_output if raw_output is not None else {}
        if step.get("status") == "succeeded" and tool:
            completed_key = (tool, step.get("sequence"))
            if completed_key not in completed:
                result["completed_stages"] = (result["completed_stages"] + [{"tool": tool, "sequence": step.get("sequence")}])[-8:]
                completed.add(completed_key)
            projected = project_result(output, max_chars=500)
            if isinstance(projected, dict):
                for key in ("media_id", "artifact_id", "execution_id", "job_id", "result_ref"):
                    if projected.get(key):
                        artifact_key = (key, str(projected[key]))
                        if artifact_key not in artifacts:
                            result["artifacts"] = (result["artifacts"] + [{"kind": key, "value": projected[key]}])[-8:]
                            artifacts.add(artifact_key)
            if tool in readable:
                # Keep one bounded, provider-neutral checkpoint.  It gives a
                # resumed model a stable handoff from a successful read to a
                # different operation without replaying the full result.
                result["last_read"] = {
                    "tool": tool,
                    "arguments": step.get("decision", {}).get("arguments", {}) if isinstance(step.get("decision"), dict) else {},
                    "result": project_result(output, max_chars=720),
                    "next_action": "Use this evidence and choose a different operation; do not repeat the same read.",
                }
            # Searching or reading an operation is evidence, not a selected
            # workflow action. Only a prepared/invoked operation with a real
            # operation id should suppress another tool directory preload.
            operation_id = projected.get("operation_id") if isinstance(projected, dict) else None
            if operation_id and tool in {
                "lynewright.integrations.prepare_invocation",
                "lynewright.integrations.invoke",
            }:
                result["selected_operation"] = {"tool": tool, "operation_id": operation_id}
        elif step.get("status") == "failed":
            result["last_blocker"] = str(step.get("error_summary") or step.get("error_code") or "")[:400]
    encoded = json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":"))
    if len(encoded) > max_chars:
        result["entities"] = result["entities"][:4]
        result["completed_stages"] = result["completed_stages"][-4:]
        result["artifacts"] = result["artifacts"][-4:]
        if result.get("last_read"):
            checkpoint = result["last_read"]
            result["last_read"] = {
                "tool": str(checkpoint.get("tool") or ""),
                "result": project_result(checkpoint.get("result"), max_chars=600),
                "next_action": str(checkpoint.get("next_action") or "")[:220],
            }
    return result
