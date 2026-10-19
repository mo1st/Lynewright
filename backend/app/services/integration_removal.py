"""Previewed removal for every non-bundled integration kind."""
from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
from typing import Any

from app.services.integration_provenance import get_provenance
from app.services.platform_services import (
    CredentialService,
    PlatformConflictError,
    PlatformNotFoundError,
    PlatformOperationError,
    PlatformStore,
)


_DELETE_TABLES = (
    # Child/dependent tables first; the connection inventory is last.
    "platform_integration_prepared_invocations",
    "platform_integration_executions",
    "platform_integration_oauth_states",
    "platform_integration_oauth_tokens",
    "platform_integration_resource_actions",
    "platform_integration_contract_actions",
    "platform_integration_mappings",
    "platform_integration_kv",
    "platform_integration_sync_state",
    "platform_integration_schedule_state",
    "platform_integration_external_resources",
    "platform_integration_capability_evidence",
    "platform_integration_provider_selection",
    "platform_integration_inbound_deliveries",
    "platform_integration_jobs",
    "platform_integration_definition_registry",
    "platform_integration_activated_definition",
    "platform_integration_bindings",
    "platform_integration_connections",
)


def _table_exists(db: Any, table: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,),
    ).fetchone() is not None


def _columns(db: Any, table: str) -> set[str]:
    return {str(row[1]) for row in db.execute(f'PRAGMA table_info("{table}")').fetchall()}


def _manifest_contracts(manifest: dict[str, Any]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for provider in manifest.get("provides", []) if isinstance(manifest.get("provides"), list) else []:
        if not isinstance(provider, dict):
            continue
        contract = str(provider.get("contract") or "")
        operations = provider.get("operations", [])
        if isinstance(operations, dict):
            pairs.update((contract, str(operation)) for operation in operations)
        elif isinstance(operations, list):
            for operation in operations:
                name = operation.get("name") if isinstance(operation, dict) else operation
                if contract and isinstance(name, str):
                    pairs.add((contract, name))
    return pairs


def _line_stages(value: Any):
    if isinstance(value, dict):
        if isinstance(value.get("contract"), str) and isinstance(value.get("operation"), str):
            yield value
        for item in value.values():
            yield from _line_stages(item)
    elif isinstance(value, list):
        for item in value:
            yield from _line_stages(item)


def _line_usage(management: Any, integration_id: str, manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    from app.services.line_engine import ProductionLineEngine

    line_store = ProductionLineEngine(store=management.store, tenant_id=management.tenant_id).db
    lines = line_store.list_lines()
    pairs = _manifest_contracts(manifest)
    with management.store.connection() as db:
        mapping_tables = [
            str(row[0]) for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'platform_integration_%'"
            ).fetchall()
        ]
        for table in mapping_tables:
            columns = _columns(db, table)
            if not {"integration_id", "target_contract", "target_operation"}.issubset(columns):
                continue
            tenant_clause = " AND tenant_id=?" if "tenant_id" in columns else ""
            args: tuple[Any, ...] = (integration_id, management.tenant_id) if tenant_clause else (integration_id,)
            for row in db.execute(
                f'SELECT target_contract,target_operation FROM "{table}" WHERE integration_id=?{tenant_clause}', args,
            ).fetchall():
                pairs.add((str(row[0]), str(row[1])))

    # Catalogue operations are the source of truth for declarative mappings.
    # Read them while the integration is still enabled; the manifest fallback
    # above also covers integrations disabled before this preview.
    from app.services.integration_operations import IntegrationOperationCatalog
    from app.services.platform_capabilities import CapabilityActor

    actor = CapabilityActor(
        client_id="integration-removal-preview", name="Removal preview",
        client_type="automation", permission_mode="full", granted_capabilities=frozenset(),
    )
    catalog = IntegrationOperationCatalog(management)
    for project_id in sorted({str(item.get("project_id") or "") for item in lines}):
        for operation in catalog.operations_for_lines(actor, project_id=project_id):
            if str(operation.get("integration_id") or "") == integration_id:
                pairs.add((str(operation.get("contract") or ""), str(operation.get("operation") or "")))

    used: list[dict[str, Any]] = []
    blocked: list[dict[str, str]] = []
    for line in lines:
        definition = line.get("definition") if isinstance(line.get("definition"), dict) else {}
        operations: list[str] = []
        for stage in _line_stages(definition):
            if str(stage.get("integration_id") or "") == integration_id or (
                str(stage.get("contract") or ""), str(stage.get("operation") or "")
            ) in pairs:
                operations.append(f"{stage.get('contract')}.{stage.get('operation')}")
        if not operations:
            continue
        line_item = {
            "id": str(line["id"]), "name": str(line.get("name") or line["id"]),
            "project_id": str(line.get("project_id") or ""),
            "enabled": bool(line.get("enabled")), "operations": sorted(set(operations)),
        }
        used.append(line_item)
        with management.store.connection() as db:
            active = db.execute(
                "SELECT 1 FROM line_runs WHERE tenant_id=? AND line_id=? AND status IN ('queued','running','waiting') LIMIT 1",
                (management.tenant_id, str(line["id"])),
            ).fetchone()
        if active:
            blocked.append({"line_id": line_item["id"], "reason": "This line has active runs. Let them finish before removing its integration."})
    return used, blocked


def _walk_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


class IntegrationRemovalService:
    def __init__(self, management: Any) -> None:
        self.management = management
        self.store: PlatformStore = management.store
        self.tenant_id = str(management.tenant_id)

    def preview(self, integration_id: str) -> dict[str, Any]:
        integration = self.management.integrations.get(integration_id)
        provenance = get_provenance(self.store, self.tenant_id, integration_id)
        bundled = str(integration.get("source") or "") == "bundled" or bool(
            provenance and provenance.get("kind") == "bundled"
        )
        if bundled:
            return {
                "integration_id": integration_id, "name": integration["name"], "bundled": True,
                "blocked": True, "blocked_reasons": ["Bundled integrations are part of Lynewright. Disable them instead."],
                "disable_lines_blocked_reasons": ["Bundled integrations are part of Lynewright. Disable them instead."],
                "lines": [], "connections": [], "mappings": {}, "runner_tokens": 0,
                "stored_wheel": None, "media": [], "settings": False, "settings_fields": [],
                "credentials": {"used": [], "exclusive": [], "shared": []},
                "listed_package": False,
            }

        lines, blocked_lines = _line_usage(self.management, integration_id, integration.get("manifest") or {})
        connections = self.management.connections.list(integration_id)
        configuration = integration.get("configuration") if isinstance(integration.get("configuration"), dict) else {}
        package = configuration.get("catalogue_package") if isinstance(configuration.get("catalogue_package"), dict) else {}
        wheel_name = str(package.get("wheel_filename") or "")
        wheel_directory = str(package.get("wheel_directory") or "")
        package_root = Path(wheel_directory).parent.parent if wheel_directory else None
        staging_paths: list[Path] = []
        if package_root is not None:
            verified_root = self._verified_package_path(str(package_root))
            if verified_root is not None and verified_root.is_dir():
                staging_paths = sorted(verified_root.glob(f".catalogue-install-{integration_id}-*"))

        mapping_counts: dict[str, int] = {}
        token_count = 0
        active_invocations = 0
        media: list[dict[str, Any]] = []
        with self.store.connection() as db:
            for table in _DELETE_TABLES:
                if not _table_exists(db, table):
                    continue
                columns = _columns(db, table)
                if not {"tenant_id", "integration_id"}.issubset(columns):
                    continue
                count = int(db.execute(
                    f'SELECT COUNT(*) FROM "{table}" WHERE tenant_id=? AND integration_id=?',
                    (self.tenant_id, integration_id),
                ).fetchone()[0])
                if count:
                    mapping_counts[table.removeprefix("platform_integration_")] = count
            if _table_exists(db, "integration_runner_tokens"):
                token_count = int(db.execute(
                    "SELECT COUNT(*) FROM integration_runner_tokens WHERE tenant_id=? AND integration_id=? AND revoked_at IS NULL",
                    (self.tenant_id, integration_id),
                ).fetchone()[0])
            if _table_exists(db, "integration_worker_invocations"):
                active_invocations = int(db.execute(
                    "SELECT COUNT(*) FROM integration_worker_invocations WHERE tenant_id=? AND integration_id=? AND status IN ('queued','claimed','running')",
                    (self.tenant_id, integration_id),
                ).fetchone()[0])
            if _table_exists(db, "platform_media"):
                for row in db.execute(
                    "SELECT id,name,metadata_json,content_type,project_id FROM platform_media WHERE tenant_id=? AND deleted_at IS NULL",
                    (self.tenant_id,),
                ).fetchall():
                    try:
                        metadata = json.loads(str(row["metadata_json"] or "{}"))
                    except ValueError:
                        metadata = {}
                    if isinstance(metadata, dict) and str(metadata.get("integration_id") or "") == integration_id:
                        media.append({
                            "id": str(row["id"]), "name": str(row["name"]),
                            "content_type": str(row["content_type"]),
                            "bytes": int(metadata.get("size_bytes") or metadata.get("bytes") or 0),
                            "stored_as": str(metadata.get("stored_as") or ""),
                        })

        credentials = CredentialService(self.store)
        known_names = {str(item.get("name")) for item in credentials.list_metadata()}
        used_names: set[str] = set()
        for connection in connections:
            used_names.update(str(item) for item in connection.get("credential_refs", []) if str(item) in known_names)
        with self.store.connection() as db:
            if _table_exists(db, "platform_integration_oauth_tokens"):
                for row in db.execute(
                    "SELECT access_credential_ref,refresh_credential_ref FROM platform_integration_oauth_tokens WHERE tenant_id=? AND integration_id=?",
                    (self.tenant_id, integration_id),
                ).fetchall():
                    used_names.update(str(row[name]) for name in ("access_credential_ref", "refresh_credential_ref") if str(row[name] or "") in known_names)
        used_names.update(value for value in _walk_strings(configuration) if value in known_names)
        exclusive: list[str] = []
        shared: list[str] = []
        for name in sorted(used_names):
            references = credentials.references(name)
            other_references = [
                item for item in references
                if integration_id not in str(item.get("label") or "")
            ]
            (shared if other_references else exclusive).append(name)

        listed_name = str(integration.get("source_distribution") or "")
        listed = bool(listed_name) and listed_name.casefold() in {
            item.strip().casefold() for item in os.getenv("LYNEWRIGHT_INTEGRATION_PACKAGES", "").split(",") if item.strip()
        }
        hard_blocked_reasons = [item["reason"] for item in blocked_lines]
        if active_invocations:
            hard_blocked_reasons.append(f"{active_invocations} runner invocation(s) are active. Wait for them to finish before removal.")
        line_names = ", ".join(
            f"{item['name']} ({item['id']})" for item in lines
        )
        reasons = list(hard_blocked_reasons)
        if lines:
            reasons.append(
                f"Lines use this integration: {line_names}. Edit them first, or choose to disable those lines."
            )
        return {
            "integration_id": integration_id, "name": integration["name"], "bundled": False,
            "blocked": bool(reasons), "blocked_reasons": reasons,
            "disable_lines_blocked_reasons": hard_blocked_reasons,
            "lines": lines, "connections": [{"id": str(item["id"]), "name": str(item.get("name") or item["id"])} for item in connections],
            "mappings": mapping_counts, "runner_tokens": token_count,
            "stored_wheel": {"filename": wheel_name, "directory": Path(wheel_directory).name if wheel_directory else ""} if wheel_name else None,
            "staging_files": [path.name for path in staging_paths],
            "media": [{key: value for key, value in item.items() if key != "stored_as"} for item in media],
            "settings": bool(configuration.get("settings")),
            "settings_fields": sorted(str(key) for key in configuration.get("settings", {}) if isinstance(key, str))
                if isinstance(configuration.get("settings"), dict) else [],
            "credentials": {"used": sorted(used_names), "exclusive": exclusive, "shared": shared},
            "listed_package": listed, "distribution": listed_name if listed else "",
            "runner_instruction": "Stop and remove this integration's runner container using your deployment tools.",
        }

    def remove(
        self, integration_id: str, *, confirm_name: str,
        disable_using_lines: bool = False, delete_exclusive_credentials: bool = False,
    ) -> dict[str, Any]:
        preview = self.preview(integration_id)
        if preview["bundled"]:
            raise PlatformConflictError("Bundled integrations are part of Lynewright. Disable them instead.")
        if confirm_name != integration_id:
            raise PlatformOperationError("Type the integration id to confirm removal.")
        if preview["disable_lines_blocked_reasons"]:
            raise PlatformConflictError(
                "Integration removal is blocked: " + " ".join(preview["disable_lines_blocked_reasons"])
            )
        if preview["lines"] and not disable_using_lines:
            raise PlatformConflictError("Integration removal is blocked: " + " ".join(preview["blocked_reasons"]))

        integration = self.management.integrations.get(integration_id)
        configuration = integration.get("configuration") if isinstance(integration.get("configuration"), dict) else {}
        package = configuration.get("catalogue_package") if isinstance(configuration.get("catalogue_package"), dict) else {}
        wheel_directory = str(package.get("wheel_directory") or "")
        wheel_path = self._verified_package_path(wheel_directory) if wheel_directory else None
        package_root = Path(wheel_directory).parent.parent if wheel_directory else None
        staging_paths: list[Path] = []
        if package_root is not None:
            verified_root = self._verified_package_path(str(package_root))
            if verified_root is not None and verified_root.is_dir():
                staging_paths = sorted(verified_root.glob(f".catalogue-install-{integration_id}-*"))
        media_rows = list(preview["media"])
        # Re-read stored names from the trusted, redacted metadata table. File
        # deletion is confined to the configured managed-media root.
        with self.store.connection() as db:
            media_files: list[str] = []
            for row in db.execute(
                "SELECT metadata_json FROM platform_media WHERE tenant_id=? AND deleted_at IS NULL",
                (self.tenant_id,),
            ).fetchall():
                try:
                    metadata = json.loads(str(row[0] or "{}"))
                except ValueError:
                    metadata = {}
                if isinstance(metadata, dict) and str(metadata.get("integration_id") or "") == integration_id:
                    stored_as = str(metadata.get("stored_as") or "")
                    if stored_as:
                        media_files.append(stored_as)

        if integration.get("enabled"):
            if self.management.lifecycle is not None and self.management.lifecycle.is_active(integration_id):
                self.management.lifecycle.disable(integration_id)
            else:
                self.management.integrations.disable(integration_id)

        removed_at = datetime.now(UTC).isoformat()
        provenance = get_provenance(self.store, self.tenant_id, integration_id)
        with self.store.connection() as db, db:
            if disable_using_lines:
                for line in preview["lines"]:
                    db.execute(
                        "UPDATE production_lines SET enabled=0,updated_at=? WHERE tenant_id=? AND id=?",
                        (datetime.now(UTC).timestamp(), self.tenant_id, line["id"]),
                    )
            if _table_exists(db, "integration_runner_tokens"):
                db.execute(
                    "UPDATE integration_runner_tokens SET revoked_at=COALESCE(revoked_at,?) WHERE tenant_id=? AND integration_id=?",
                    (removed_at, self.tenant_id, integration_id),
                )
            for table in _DELETE_TABLES:
                if not _table_exists(db, table):
                    continue
                columns = _columns(db, table)
                if {"tenant_id", "integration_id"}.issubset(columns):
                    db.execute(
                        f'DELETE FROM "{table}" WHERE tenant_id=? AND integration_id=?',
                        (self.tenant_id, integration_id),
                    )
            if _table_exists(db, "platform_media"):
                for media_item in media_rows:
                    db.execute(
                        "UPDATE platform_media SET deleted_at=? WHERE tenant_id=? AND id=?",
                        (removed_at, self.tenant_id, media_item["id"]),
                    )
                    if _table_exists(db, "platform_media_links"):
                        db.execute(
                            "UPDATE platform_media_links SET revoked_at=COALESCE(revoked_at,?) WHERE tenant_id=? AND media_id=?",
                            (removed_at, self.tenant_id, media_item["id"]),
                        )
            db.execute(
                """CREATE TABLE IF NOT EXISTS integration_removal_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,
                    integration_id TEXT NOT NULL, integration_name TEXT NOT NULL,
                    removed_by TEXT NOT NULL, removed_at TEXT NOT NULL,
                    provenance_json TEXT NOT NULL DEFAULT '{}', details_json TEXT NOT NULL DEFAULT '{}'
                )"""
            )
            db.execute(
                """INSERT INTO integration_removal_audit
                   (tenant_id,integration_id,integration_name,removed_by,removed_at,provenance_json,details_json)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    self.tenant_id, integration_id, str(integration["name"]),
                    str(self.management.actor_id), removed_at,
                    json.dumps(provenance or {}, sort_keys=True),
                    json.dumps({
                        "disabled_lines": [item["id"] for item in preview["lines"]] if disable_using_lines else [],
                        "deleted_media": [item["id"] for item in media_rows],
                        "deleted_connections": [item["id"] for item in preview["connections"]],
                        "package_distribution": integration.get("source_distribution") or "",
                    }, sort_keys=True),
                ),
            )
            db.execute(
                "DELETE FROM platform_extensions WHERE tenant_id=? AND id=?",
                (self.tenant_id, integration_id),
            )

        from app.services.production_integrations import mark_integration_removed_live
        mark_integration_removed_live(self.tenant_id, integration_id)
        if self.management.lifecycle is not None:
            self.management.lifecycle.unregister_definition(integration_id)

        if wheel_path is not None:
            if wheel_path.is_symlink():
                wheel_path.unlink(missing_ok=True)
            elif wheel_path.is_dir():
                shutil.rmtree(wheel_path)
            for parent in (wheel_path.parent, wheel_path.parent.parent):
                try:
                    parent.rmdir()
                except OSError:
                    pass
        for staging_path in staging_paths:
            verified_staging = self._verified_package_path(str(staging_path))
            if verified_staging is None:
                continue
            if verified_staging.is_symlink():
                verified_staging.unlink(missing_ok=True)
            elif verified_staging.is_dir():
                shutil.rmtree(verified_staging)

        if media_files:
            from app.services.integration_media_transfer import IntegrationMediaTransferService, managed_media_root

            transfer = IntegrationMediaTransferService(temp_dir=managed_media_root())
            for stored_as in media_files:
                transfer.delete_managed_copy(stored_as)

        deleted_credentials: list[str] = []
        if delete_exclusive_credentials:
            credentials = CredentialService(self.store)
            for name in preview["credentials"]["exclusive"]:
                credentials.delete(name, confirm_name=name)
                deleted_credentials.append(name)

        return {
            "removed": True, "integration_id": integration_id,
            "disabled_lines": [item["id"] for item in preview["lines"]] if disable_using_lines else [],
            "deleted_media": len(media_rows), "deleted_credentials": deleted_credentials,
            "runner_instruction": preview["runner_instruction"],
            "listed_package_note": (
                f"Remove {preview['distribution']} from LYNEWRIGHT_INTEGRATION_PACKAGES or it will be discovered again on the next start."
                if preview["listed_package"] else None
            ),
        }

    def _verified_package_path(self, path_value: str) -> Path | None:
        from app.config import get_settings

        root = get_settings().packages_dir.resolve()
        candidate = Path(path_value)
        if not candidate.is_absolute():
            return None
        # Validate the unresolved path first, then the resolved path. A
        # symlink that points outside the managed packages root is never
        # followed or recursively removed.
        if candidate.is_symlink():
            resolved_parent = candidate.parent.resolve()
            if resolved_parent != root and root not in resolved_parent.parents:
                raise PlatformOperationError("Stored package path is outside the managed package directory.")
            return candidate
        resolved = candidate.resolve()
        if resolved != root and root not in resolved.parents:
            raise PlatformOperationError("Stored package path is outside the managed package directory.")
        return resolved


__all__ = ["IntegrationRemovalService"]
