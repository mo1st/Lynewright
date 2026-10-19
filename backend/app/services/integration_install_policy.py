"""Shared install confirmation and outside-install policy decisions."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import sqlite3
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.config import get_settings
from app.services.integration_provenance import ensure_schema
from app.services.platform_services import PlatformConflictError, PlatformOperationError, PlatformStore


@dataclass(frozen=True)
class InstallDecision:
    confirmed_by: str | None
    confirmed_at: str | None
    summary_hash: str
    policy: str
    catalogue_matched: bool


class InstallConfirmationRequired(PlatformConflictError):
    def __init__(self, summary: dict[str, Any], *, summary_hash: str, message: str = "Confirm this install before it can be installed.") -> None:
        super().__init__(message)
        self.summary = summary
        self.summary_hash = summary_hash

    def response(self) -> dict[str, Any]:
        return {
            "requires_confirmation": True,
            "install_summary": self.summary,
            "summary_hash": self.summary_hash,
            "message": self.message,
        }


class InstallPolicyForbidden(PlatformOperationError):
    def __init__(self) -> None:
        super().__init__("LYNEWRIGHT_INSTALL_POLICY=catalogue-only blocks integrations that do not exactly match the signed catalogue.")


def current_install_policy() -> str:
    """Validate policy on every security-sensitive read; startup also calls this."""
    return get_settings().install_policy


def summary_hash(summary: dict[str, Any]) -> str:
    encoded = json.dumps(summary, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_source_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        # Credentials and query values are not safe to put in logs or a dialog.
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
    except ValueError:
        return "[invalid URL omitted]"


def catalogue_status(
    store: PlatformStore,
    *, integration_id: str,
    version: str,
    artifact_kind: str,
    sha256: str = "",
) -> str:
    """Classify against the last verified catalogue without trusting request data."""
    try:
        with store.connection() as db:
            ensure_schema(db)
            row = db.execute(
                "SELECT index_bytes,verified_key_id FROM integration_catalogue_cache WHERE id=1"
            ).fetchone()
        index = json.loads(bytes(row["index_bytes"]).decode("utf-8")) if row and row["index_bytes"] else {}
    except (OSError, TypeError, ValueError, UnicodeError, json.JSONDecodeError, sqlite3.OperationalError):
        index = {}
        row = None
    if row is not None:
        from app.services.catalogue import is_signing_key_trusted
        if not is_signing_key_trusted(str(row["verified_key_id"] or "")):
            index = {}
    entries = [
        item for item in index.get("entries", [])
        if isinstance(item, dict) and str(item.get("id") or "") == integration_id
    ] if isinstance(index, dict) else []
    if not entries:
        return "not listed"
    matching_version = [item for item in entries if str(item.get("version") or "") == version]
    for item in matching_version:
        if item.get("yanked") is not None:
            return "yanked"
    for item in matching_version:
        if item.get("kind") == artifact_kind and sha256 and str(item.get("sha256") or "") == sha256:
            return "matched"
    return "id listed but hash differs"


def build_install_summary(
    artifact: dict[str, Any],
    *, kind: str,
    source: str,
    sha256: str = "",
    catalogue_status_value: str = "not listed",
) -> dict[str, Any]:
    """Build the confirmation view from the validated, server-side artifact."""
    integration_id = str(artifact.get("id") or "")
    name = str(artifact.get("name") or integration_id)
    version = str(artifact.get("version") or "")
    is_package = kind == "native package"
    is_container = kind == "container image"
    if is_package:
        if __import__("os").getenv("LYNEWRIGHT_ALLOW_INPROCESS_PACKAGES", "").strip() == "1":
            execution = "runs INSIDE Lynewright with access to all data"
        else:
            execution = "runs in its own runner container"
    elif is_container:
        execution = "runs in its own container"
    else:
        execution = "declarative integration; no code runs"
    requested = artifact.get("requested_capabilities")
    permissions = artifact.get("permissions") if isinstance(artifact.get("permissions"), dict) else {}
    if not isinstance(requested, list):
        requested = permissions.get("capabilities", [])
    credentials = artifact.get("credentials", [])
    credential_refs = [
        str(item.get("name")) for item in credentials
        if isinstance(item, dict) and item.get("name")
    ] if isinstance(credentials, list) else []
    if not credential_refs:
        references = artifact.get("credential_refs", [])
        credential_refs = [str(item) for item in references if isinstance(item, str) and item] if isinstance(references, list) else []
    media_access = permissions.get("media_access")
    if media_access is None:
        media_access = [item for item in requested if isinstance(item, str) and item.startswith("media:")]
    egress = permissions.get("egress", artifact.get("egress"))
    if egress is None:
        egress = "configured endpoint" if kind == "declarative file" else "not declared"
    if not isinstance(egress, str):
        egress = json.dumps(egress, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    declared_source = artifact.get("source") or artifact.get("endpoint_url")
    if isinstance(declared_source, dict):
        declared_source = declared_source.get("url") or declared_source.get("name") or ""
    safe_declared_source = _safe_source_url(declared_source) if str(declared_source or "").startswith(("http://", "https://")) else str(declared_source or "")
    return {
        "integration_id": integration_id,
        "name": name,
        "version": version,
        "kind": kind,
        "source": source,
        "declared_source": safe_declared_source,
        "sha256": sha256 or None,
        "runs_code": bool(is_package or is_container),
        "execution": execution,
        "requested_capabilities": sorted({str(item) for item in requested}) if isinstance(requested, list) else [],
        "egress": egress,
        "credential_refs": sorted(set(credential_refs)),
        "media_access": media_access,
        "catalogue_status": catalogue_status_value,
    }


def decide_install(
    summary: dict[str, Any],
    *,
    confirm: dict[str, Any] | None,
    actor: str,
    catalogue_matched: bool,
) -> InstallDecision:
    policy = current_install_policy()
    digest = summary_hash(summary)
    if catalogue_matched:
        return InstallDecision(None, None, digest, policy, True)
    if policy == "catalogue-only":
        raise InstallPolicyForbidden()
    if policy == "confirm":
        expected_id = str(summary.get("integration_id") or "")
        supplied_id = str(confirm.get("integration_id") or "") if isinstance(confirm, dict) else ""
        supplied_hash = str(confirm.get("summary_hash") or "") if isinstance(confirm, dict) else ""
        if supplied_id != expected_id or supplied_hash != digest:
            message = "Type the integration id and confirm the current install summary." if not confirm else "The confirmation id or summary hash is stale; review the current summary and try again."
            raise InstallConfirmationRequired(summary, summary_hash=digest, message=message)
    now = datetime.now(UTC).isoformat()
    confirmed_by = str(actor or "unknown")[:200]
    if policy == "open":
        confirmed_by = confirmed_by or "admin:LYNEWRIGHT_INSTALL_POLICY=open"
    return InstallDecision(confirmed_by, now, digest, policy, False)


def log_install_confirmation(
    *, integration_id: str,
    actor: str,
    decision: InstallDecision,
) -> None:
    """Write a secret-free audit record after the install succeeds."""
    from app.services.application_logging import log_event

    log_event(
        level="warning" if decision.policy == "open" else "info",
        category="integration_install",
        source="install_confirmation",
        message=(
            "Outside integration installed under the open policy; confirmation was waived."
            if decision.policy == "open"
            else "Outside integration installation was confirmed."
        ),
        details={
            "integration_id": integration_id,
            "confirmed_by": actor,
            "policy": decision.policy,
            "summary_hash": decision.summary_hash,
        },
    )

