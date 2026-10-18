"""Single-writer provenance records for installed integrations."""
from __future__ import annotations

from datetime import UTC, datetime
import json
import sqlite3
from typing import Any

from app.services.platform_services import PlatformStore


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS integration_provenance (
            tenant_id TEXT NOT NULL,
            integration_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('bundled','catalogue','outside')),
            status TEXT NOT NULL DEFAULT 'current',
            source TEXT,
            confirmed_by TEXT,
            confirmed_at TEXT,
            recorded_at TEXT NOT NULL,
            catalogue_entry_id TEXT,
            entry_version TEXT,
            sha256 TEXT,
            image_digest TEXT,
            catalogue_serial INTEGER,
            signing_key_id TEXT,
            reviewer TEXT,
            review_date TEXT,
            drift_reason TEXT,
            PRIMARY KEY (tenant_id, integration_id)
        )
        """
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _public(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    value = dict(row)
    review = None
    if value.get("reviewer") or value.get("review_date"):
        review = {"reviewer": value.get("reviewer"), "date": value.get("review_date")}
    return {
        "kind": value["kind"],
        "status": value.get("status") or "current",
        "source": value.get("source"),
        "confirmed_by": value.get("confirmed_by"),
        "confirmed_at": value.get("confirmed_at"),
        "recorded_at": value.get("recorded_at"),
        "catalogue_entry_id": value.get("catalogue_entry_id"),
        "entry_version": value.get("entry_version"),
        "sha256": value.get("sha256"),
        "image_digest": value.get("image_digest"),
        "catalogue_serial": value.get("catalogue_serial"),
        "signing_key_id": value.get("signing_key_id"),
        "review": review,
        "drift_reason": value.get("drift_reason"),
    }


def get_provenance(store: PlatformStore, tenant_id: str, integration_id: str) -> dict[str, Any] | None:
    with store.connection() as db:
        ensure_schema(db)
        row = db.execute(
            "SELECT * FROM integration_provenance WHERE tenant_id=? AND integration_id=?",
            (tenant_id, integration_id),
        ).fetchone()
    return _public(row)


def list_provenance(
    store: PlatformStore, *, tenant_id: str | None = None, kind: str | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    values: list[Any] = []
    if tenant_id is not None:
        clauses.append("tenant_id=?")
        values.append(tenant_id)
    if kind is not None:
        clauses.append("kind=?")
        values.append(kind)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with store.connection() as db:
        ensure_schema(db)
        rows = db.execute(
            "SELECT * FROM integration_provenance" + where + " ORDER BY tenant_id,integration_id",
            values,
        ).fetchall()
    return [
        {"tenant_id": str(row["tenant_id"]), "integration_id": str(row["integration_id"]), **(_public(row) or {})}
        for row in rows
    ]


def _catalogue_file_match(
    db: sqlite3.Connection, integration_id: str, entry_version: str, sha256: str,
    *, artifact_kind: str = "file",
) -> tuple[dict[str, Any], int, str] | None:
    try:
        cached = db.execute(
            "SELECT index_bytes,serial,verified_key_id FROM integration_catalogue_cache WHERE id=1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if cached is None or not cached["index_bytes"] or cached["serial"] is None:
        return None
    key_id = str(cached["verified_key_id"] or "")
    from app.services.catalogue import is_signing_key_trusted
    if not is_signing_key_trusted(key_id):
        return None
    try:
        index = json.loads(bytes(cached["index_bytes"]).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, TypeError):
        return None
    for entry in index.get("entries", []):
        if (
            isinstance(entry, dict)
            and entry.get("kind") == artifact_kind
            and entry.get("id") == integration_id
            and entry.get("version") == entry_version
            and entry.get("sha256") == sha256
        ):
            return entry, int(cached["serial"]), key_id
    return None


def record_provenance(
    store: PlatformStore,
    tenant_id: str,
    integration_id: str,
    *,
    kind: str | None,
    source: str | None = None,
    confirmed_by: str | None = None,
    confirmed_at: str | None = None,
    recorded_at: str | None = None,
    catalogue_entry_id: str | None = None,
    entry_version: str | None = None,
    sha256: str | None = None,
    image_digest: str | None = None,
    catalogue_serial: int | None = None,
    signing_key_id: str | None = None,
    reviewer: str | None = None,
    review_date: str | None = None,
    status: str = "current",
    drift_reason: str | None = None,
) -> dict[str, Any] | None:
    """The only function that inserts, updates, or removes provenance rows.

    Outside declarative file imports become catalogue provenance only when id,
    version, kind=file, and the exact imported-byte SHA-256 match the verified
    cached index. Passing kind=None removes a record as part of uninstall rollback.
    """
    if kind is not None and kind not in {"bundled", "catalogue", "outside"}:
        raise ValueError("Integration provenance kind must be bundled, catalogue, outside, or None.")
    with store.connection() as db, db:
        ensure_schema(db)
        if kind is None:
            db.execute(
                "DELETE FROM integration_provenance WHERE tenant_id=? AND integration_id=?",
                (tenant_id, integration_id),
            )
            return None
        if kind == "outside" and sha256:
            artifact_kind = "package" if str(source or "").startswith("package:") else "file"
            match = _catalogue_file_match(
                db, integration_id, str(entry_version or ""), sha256,
                artifact_kind=artifact_kind,
            )
            if match is not None:
                entry, catalogue_serial, signing_key_id = match
                yanked = entry.get("yanked")
                # A yanked artifact is no longer a catalogue-approved install.
                # Keep its origin outside so policy and confirmation still apply.
                if yanked is None:
                    review = entry.get("review") if isinstance(entry.get("review"), dict) else {}
                    kind = "catalogue"
                    source = None
                    confirmed_by = None
                    confirmed_at = None
                    catalogue_entry_id = str(entry["id"])
                    entry_version = str(entry["version"])
                    image_digest = None
                    reviewer = str(review.get("reviewer") or "") or None
                    review_date = str(review.get("date") or "") or None
                    status = "current"
                    drift_reason = None
        if kind == "catalogue":
            if not catalogue_entry_id or not entry_version or not (sha256 or image_digest):
                raise ValueError("Catalogue provenance requires entry id, version, and an artifact hash or digest.")
            if catalogue_serial is None or not signing_key_id:
                raise ValueError("Catalogue provenance requires its signed serial and signing key id.")
            if not reviewer or not review_date:
                raise ValueError("Catalogue provenance requires catalogue review metadata.")
            source = None
            confirmed_by = None
            confirmed_at = None
        elif kind == "outside":
            if not source:
                raise ValueError("Outside provenance requires a source description.")
            if status == "awaiting_confirmation":
                confirmed_by = None
                confirmed_at = None
            else:
                confirmed_by = confirmed_by or "unknown"
            catalogue_entry_id = entry_version = image_digest = signing_key_id = reviewer = review_date = None
            catalogue_serial = None
        else:
            source = confirmed_by = confirmed_at = None
            catalogue_entry_id = entry_version = sha256 = image_digest = signing_key_id = reviewer = review_date = None
            catalogue_serial = None
            status = "current"
            drift_reason = None
        stamp = recorded_at or _now()
        db.execute(
            """INSERT INTO integration_provenance (
                 tenant_id,integration_id,kind,status,source,confirmed_by,confirmed_at,recorded_at,
                 catalogue_entry_id,entry_version,sha256,image_digest,catalogue_serial,signing_key_id,
                 reviewer,review_date,drift_reason
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(tenant_id,integration_id) DO UPDATE SET
                 kind=excluded.kind,status=excluded.status,source=excluded.source,
                 confirmed_by=excluded.confirmed_by,confirmed_at=excluded.confirmed_at,
                 recorded_at=excluded.recorded_at,catalogue_entry_id=excluded.catalogue_entry_id,
                 entry_version=excluded.entry_version,sha256=excluded.sha256,image_digest=excluded.image_digest,
                 catalogue_serial=excluded.catalogue_serial,signing_key_id=excluded.signing_key_id,
                 reviewer=excluded.reviewer,review_date=excluded.review_date,drift_reason=excluded.drift_reason""",
            (
                tenant_id, integration_id, kind, status, source, confirmed_by, confirmed_at, stamp,
                catalogue_entry_id, entry_version, sha256, image_digest, catalogue_serial, signing_key_id,
                reviewer, review_date, drift_reason,
            ),
        )
        row = db.execute(
            "SELECT * FROM integration_provenance WHERE tenant_id=? AND integration_id=?",
            (tenant_id, integration_id),
        ).fetchone()
    return _public(row)


def backfill_pre_k52_provenance(store: PlatformStore | None = None) -> int:
    """Classify old rows conservatively; never infer catalogue membership."""
    store = store or PlatformStore()
    with store.connection() as db:
        ensure_schema(db)
        rows = db.execute(
            "SELECT tenant_id,id,source,installed_at FROM platform_extensions ORDER BY tenant_id,id"
        ).fetchall()
        known = {
            (str(row["tenant_id"]), str(row["integration_id"]))
            for row in db.execute("SELECT tenant_id,integration_id FROM integration_provenance").fetchall()
        }
    try:
        from app.services.production_integrations import discover_shipped_manifests
        bundled_ids = {
            str(item.manifest.get("id"))
            for item in discover_shipped_manifests()
            if item.source == "bundled"
        }
    except Exception:
        bundled_ids = set()
    count = 0
    for row in rows:
        tenant_id, integration_id = str(row["tenant_id"]), str(row["id"])
        if (tenant_id, integration_id) in known:
            continue
        if str(row["source"] or "") == "bundled" or integration_id in bundled_ids:
            record_provenance(store, tenant_id, integration_id, kind="bundled")
        else:
            record_provenance(
                store, tenant_id, integration_id, kind="outside",
                source="pre-K5.2 install, unknown", confirmed_by="unknown",
            )
        count += 1
    return count


def reconcile_catalogue_drift(
    store: PlatformStore,
    index: dict[str, Any],
    serial: int,
    *,
    trusted_signing_key_ids: set[str] | None = None,
) -> int:
    """Flag historical catalogue installs after a successful signed refresh."""
    del serial  # Installed provenance retains the original signed serial.
    entries = {
        (str(entry.get("id") or ""), str(entry.get("version") or "")): entry
        for entry in index.get("entries", []) if isinstance(entry, dict)
    }
    catalogue_rows = list_provenance(store, kind="catalogue")
    changed = 0
    for row in catalogue_rows:
        signing_key_id = str(row.get("signing_key_id") or "")
        key_is_trusted = True
        if trusted_signing_key_ids is not None:
            # K5.5 provenance stored dev ids without their prefix. Keep those
            # historical rows recognizable, but only trust them with opt-in.
            key_is_trusted = (
                signing_key_id in trusted_signing_key_ids
                or f"dev:{signing_key_id.removeprefix('dev:')}" in trusted_signing_key_ids
            )
        entry = entries.get((str(row["catalogue_entry_id"]), str(row["entry_version"])))
        reason: str | None = None
        if not key_is_trusted:
            reason = "signing key no longer trusted"
        elif entry is None:
            reason = "Catalogue entry is missing."
        elif "yanked" in entry:
            yanked = entry.get("yanked")
            reason = f"Catalogue entry is yanked: {yanked.get('reason', 'no reason supplied')}" if isinstance(yanked, dict) else "Catalogue entry is yanked."
        elif row.get("sha256") and entry.get("sha256") != row.get("sha256"):
            reason = f"Catalogue artifact sha256 changed from {row['sha256']} to {entry.get('sha256') or 'missing'}."
        elif row.get("image_digest") and str(entry.get("source") or "").split("@", 1)[-1] != row.get("image_digest"):
            reason = "Catalogue container image digest changed."
        old_status = row.get("status")
        new_status = "catalogue_drift" if reason else "current"
        if old_status == new_status and row.get("drift_reason") == reason:
            continue
        review = row.get("review") or {}
        record_provenance(
            store, str(row["tenant_id"]), str(row["integration_id"]),
            kind="catalogue", catalogue_entry_id=row.get("catalogue_entry_id"),
            entry_version=row.get("entry_version"), sha256=row.get("sha256"),
            image_digest=row.get("image_digest"), catalogue_serial=row.get("catalogue_serial"),
            signing_key_id=row.get("signing_key_id"), reviewer=review.get("reviewer"),
            review_date=review.get("date"), recorded_at=row.get("recorded_at"),
            status=new_status, drift_reason=reason,
        )
        changed += 1
    return changed
