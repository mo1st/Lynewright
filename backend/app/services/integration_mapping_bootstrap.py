"""Bounded, provider-neutral helpers for live resource-mapping bootstrap."""
from __future__ import annotations

import json
import re
from typing import Any

from app.services.platform_services import _redact


MAX_MAPPING_SAMPLE_BYTES = 64 * 1024
MAX_PROBE_RESULT_BYTES = 256 * 1024
MAX_SUGGESTION_DEPTH = 6
MAX_SUGGESTION_NODES = 2_000
MAX_SUGGESTION_LIST_ITEMS = 25
MAX_SUGGESTION_SAMPLE_BYTES = 256 * 1024
_PATH_SEGMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,79}$")

_ITEM_HINTS = {"items", "results", "records", "data", "entries", "objects", "list"}
_ID_HINTS = {"id", "key", "uid", "uuid", "external_id", "externalid"}
_NAME_HINTS = {"name", "title", "label", "display_name", "displayname"}
_URL_HINTS = {"url", "uri", "link", "href"}
_STATUS_HINTS = {"status", "state", "phase", "lifecycle"}


def bounded_json(value: Any, *, max_bytes: int) -> tuple[Any, bool, int]:
    """Return redacted JSON that is always bounded for operator display."""
    redacted = _redact(value)
    try:
        encoded = json.dumps(redacted, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        encoded = json.dumps(str(redacted), ensure_ascii=False, separators=(",", ":"))
    size = len(encoded.encode("utf-8"))
    if size <= max_bytes:
        return redacted, False, size
    encoded_bytes = encoded.encode("utf-8")
    prefix = encoded_bytes[:max_bytes].decode("utf-8", errors="ignore")
    truncated = {"_truncated": True, "raw_prefix": prefix}
    while len(json.dumps(truncated, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > max_bytes and prefix:
        prefix = prefix[:-256]
        truncated["raw_prefix"] = prefix
    return truncated, True, size


def suggest_mapping_paths(sample: Any) -> dict[str, list[dict[str, Any]]]:
    """Suggest bounded JSON paths without knowing anything about a provider."""
    try:
        if len(json.dumps(sample, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")) > MAX_SUGGESTION_SAMPLE_BYTES:
            return _empty_suggestions()
    except (TypeError, ValueError, RecursionError):
        return _empty_suggestions()
    lists: list[tuple[str, list[Any]]] = []
    nodes = 0

    def visit(value: Any, path: str, depth: int) -> None:
        nonlocal nodes
        if nodes >= MAX_SUGGESTION_NODES or depth > MAX_SUGGESTION_DEPTH:
            return
        nodes += 1
        if isinstance(value, list):
            lists.append((path, value))
            for item in value[:MAX_SUGGESTION_LIST_ITEMS]:
                if isinstance(item, dict):
                    visit(item, path, depth + 1)
            return
        if not isinstance(value, dict):
            return
        for key, item in list(value.items())[:MAX_SUGGESTION_LIST_ITEMS]:
            key_text = str(key)
            if not _PATH_SEGMENT.fullmatch(key_text):
                continue
            child = key_text if path == "$" else f"{path}.{key_text}"
            visit(item, child, depth + 1)

    visit(sample, "$", 0)
    item_candidates: list[dict[str, Any]] = []
    field_candidates: dict[str, list[dict[str, Any]]] = {key: [] for key in ("id_path", "name_path", "url_path", "status_path")}
    seen_items: set[str] = set()
    seen_fields: set[tuple[str, str]] = set()
    for path, values in lists:
        objects = [item for item in values[:MAX_SUGGESTION_LIST_ITEMS] if isinstance(item, dict)]
        if not objects or path in seen_items:
            continue
        seen_items.add(path)
        tail = path.rsplit(".", 1)[-1].lower() if path != "$" else "root"
        score = 80 + (15 if tail in _ITEM_HINTS else 0) + min(len(objects), 5)
        item_candidates.append({"path": path, "score": score, "reason": "List of JSON objects suitable for resource items."})
        fields = _scalar_fields(objects)
        for field_path, key in fields:
            normalized = key.lower().replace("-", "_")
            for target, hints in (
                ("id_path", _ID_HINTS),
                ("name_path", _NAME_HINTS),
                ("url_path", _URL_HINTS),
                ("status_path", _STATUS_HINTS),
            ):
                matches = normalized in hints or (target == "id_path" and normalized.endswith("id"))
                if not matches:
                    continue
                marker = (target, field_path)
                if marker in seen_fields:
                    continue
                seen_fields.add(marker)
                field_candidates[target].append({
                    "path": field_path,
                    "score": 100 if normalized in hints else 85,
                    "reason": f"Scalar field name matches the generic {target.removesuffix('_path')} vocabulary.",
                })

    item_candidates.sort(key=lambda item: (-int(item["score"]), str(item["path"])))
    for values in field_candidates.values():
        values.sort(key=lambda item: (-int(item["score"]), str(item["path"])))
    return {"items_path": item_candidates[:10], **{key: values[:10] for key, values in field_candidates.items()}}


def _empty_suggestions() -> dict[str, list[dict[str, Any]]]:
    return {key: [] for key in ("items_path", "id_path", "name_path", "url_path", "status_path")}


def _scalar_fields(objects: list[dict[str, Any]]) -> list[tuple[str, str]]:
    per_item: list[dict[str, str]] = []

    def visit(value: Any, path: str, depth: int, fields: dict[str, str], nodes: list[int]) -> None:
        if nodes[0] >= MAX_SUGGESTION_NODES:
            return
        nodes[0] += 1
        if depth > MAX_SUGGESTION_DEPTH:
            return
        if not isinstance(value, dict):
            return
        for key, item in list(value.items())[:MAX_SUGGESTION_LIST_ITEMS]:
            key_text = str(key)
            if not _PATH_SEGMENT.fullmatch(key_text):
                continue
            child = key_text if not path else f"{path}.{key_text}"
            if isinstance(item, (str, int, float, bool)) or item is None:
                fields.setdefault(child, key_text)
            elif isinstance(item, dict):
                visit(item, child, depth + 1, fields, nodes)

    for item in objects:
        fields: dict[str, str] = {}
        visit(item, "", 0, fields, [0])
        per_item.append(fields)
    if not per_item:
        return []
    common = set(per_item[0])
    for fields in per_item[1:]:
        common.intersection_update(fields)
    return [(path, per_item[0][path]) for path in sorted(common)]
