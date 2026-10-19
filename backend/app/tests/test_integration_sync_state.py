from __future__ import annotations

import sqlite3

from app.services.integration_state import IntegrationStateService
from app.services.platform_services import PlatformStore


def test_mapping_reverse_lookup_and_incremental_checkpoint_are_scoped() -> None:
    db = sqlite3.connect(":memory:"); db.row_factory = sqlite3.Row
    state = IntegrationStateService(PlatformStore(lambda: db))
    scope = {"integration_id": "lynewright.integration.mock", "connection_id": "primary", "project_id": "project-a"}
    state.put_mapping(scope, external_type="remote.item", external_id="remote-1", lynewright_type="project", lynewright_id="project-a", metadata={"source": "sync"})
    assert state.find_mapping_by_lynewright(scope, "project", "project-a")["external_id"] == "remote-1"
    checkpoint = state.set_sync_checkpoint(scope, cursor="cursor-2", etag="etag-2", revision="42")
    assert checkpoint["state"] == {"cursor": "cursor-2", "etag": "etag-2", "revision": "42", "incremental": True}
    assert state.get_sync_state({**scope, "project_id": "project-b"})["state"] == {}
    db.close()


def test_mappings_support_global_and_independent_optional_project_scopes() -> None:
    db = sqlite3.connect(":memory:"); db.row_factory = sqlite3.Row
    state = IntegrationStateService(PlatformStore(lambda: db))
    base = {"integration_id": "lynewright.integration.mock", "connection_id": "primary"}
    state.put_mapping(base, external_type="remote.item", external_id="same", lynewright_type="project", lynewright_id="global")
    state.put_mapping({**base, "project_id": "project-a"}, external_type="remote.item", external_id="same", lynewright_type="project", lynewright_id="a")
    state.put_mapping({**base, "project_id": "project-b"}, external_type="remote.item", external_id="same", lynewright_type="project", lynewright_id="b")
    assert state.get_mapping(base, "remote.item", "same")["lynewright_id"] == "global"
    assert state.get_mapping({**base, "project_id": "project-a"}, "remote.item", "same")["lynewright_id"] == "a"
    assert state.get_mapping({**base, "project_id": "project-b"}, "remote.item", "same")["lynewright_id"] == "b"
    db.close()


def test_legacy_global_mapping_table_is_preserved_during_project_scope_migration() -> None:
    db = sqlite3.connect(":memory:"); db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE platform_integration_mappings (
            tenant_id TEXT NOT NULL, integration_id TEXT NOT NULL, connection_id TEXT NOT NULL DEFAULT '',
            external_type TEXT NOT NULL, external_id TEXT NOT NULL, lynewright_type TEXT NOT NULL,
            lynewright_id TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (tenant_id, integration_id, connection_id, external_type, external_id)
        );
        INSERT INTO platform_integration_mappings
        (tenant_id,integration_id,connection_id,external_type,external_id,lynewright_type,lynewright_id,metadata_json)
        VALUES ('ten_default','lynewright.integration.mock','primary','remote.item','legacy','project','global','{}');
        """
    )
    state = IntegrationStateService(PlatformStore(lambda: db))
    mapping = state.get_mapping({"integration_id": "lynewright.integration.mock", "connection_id": "primary"}, "remote.item", "legacy")
    assert mapping["project_id"] == ""
    assert mapping["lynewright_id"] == "global"
    db.close()
