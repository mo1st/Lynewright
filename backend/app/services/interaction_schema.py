"""Interaction/notification gateway storage.

Idempotent DDL for the provider-neutral interaction layer (Session F): channel
connections, secure account links, conversations, messages, approval/action
transport, the generic inbound-request/outbound-response boundary, and
notification routes/deliveries.

The matching ``.sql`` file is a reference mirror that nothing executes, which
is why every statement below is safe to run repeatedly.
"""

from __future__ import annotations

import sqlite3


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}


def _add_columns(db: sqlite3.Connection, table: str, definitions: dict[str, str]) -> None:
    existing = _columns(db, table)
    for name, definition in definitions.items():
        if name not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


_DDL = """
CREATE TABLE IF NOT EXISTS interaction_connections (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    channel_type TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    external_identity TEXT NOT NULL DEFAULT '',
    credentials_json TEXT NOT NULL DEFAULT '{}',
    webhook_secret_hash TEXT NOT NULL DEFAULT '',
    operator_profile_id TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    verified_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
    FOREIGN KEY (project_id) REFERENCES tenant_projects(project_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_interaction_connections_tenant
    ON interaction_connections(tenant_id, project_id, channel_type);

CREATE TABLE IF NOT EXISTS interaction_channel_links (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    connection_id TEXT NOT NULL REFERENCES interaction_connections(id) ON DELETE CASCADE,
    channel_type TEXT NOT NULL,
    external_user_id TEXT NOT NULL,
    external_username TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at TEXT,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
    FOREIGN KEY (project_id) REFERENCES tenant_projects(project_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_interaction_links_active
    ON interaction_channel_links(connection_id, external_user_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_interaction_links_tenant
    ON interaction_channel_links(tenant_id, project_id, status);

CREATE TABLE IF NOT EXISTS interaction_link_tokens (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    connection_id TEXT NOT NULL REFERENCES interaction_connections(id) ON DELETE CASCADE,
    expires_at_epoch REAL NOT NULL,
    consumed_at_epoch REAL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_interaction_link_tokens_hash
    ON interaction_link_tokens(token_hash);

CREATE TABLE IF NOT EXISTS interaction_conversations (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    connection_id TEXT NOT NULL REFERENCES interaction_connections(id) ON DELETE CASCADE,
    channel_type TEXT NOT NULL,
    link_id TEXT REFERENCES interaction_channel_links(id) ON DELETE SET NULL,
    external_chat_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'active',
    active_run_id TEXT,
    run_state TEXT,
    last_inbound_at TEXT,
    last_outbound_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
    FOREIGN KEY (project_id) REFERENCES tenant_projects(project_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_interaction_conversations_chat
    ON interaction_conversations(connection_id, external_chat_id);
CREATE INDEX IF NOT EXISTS idx_interaction_conversations_scope
    ON interaction_conversations(tenant_id, project_id, channel_type, state);

CREATE TABLE IF NOT EXISTS interaction_messages (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES interaction_conversations(id) ON DELETE CASCADE,
    direction TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'system',
    message_type TEXT NOT NULL DEFAULT 'text',
    text TEXT NOT NULL DEFAULT '',
    media_json TEXT NOT NULL DEFAULT '[]',
    action_id TEXT,
    external_message_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'sent',
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_interaction_messages_conversation
    ON interaction_messages(conversation_id, created_at, id);
CREATE INDEX IF NOT EXISTS idx_interaction_messages_external
    ON interaction_messages(conversation_id, direction, external_message_id);

CREATE TABLE IF NOT EXISTS interaction_processed_updates (
    connection_id TEXT NOT NULL,
    update_id INTEGER NOT NULL,
    processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (connection_id, update_id)
);

CREATE TABLE IF NOT EXISTS interaction_actions (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES interaction_conversations(id) ON DELETE CASCADE,
    run_id TEXT,
    action_type TEXT NOT NULL DEFAULT 'approval.request',
    decision_options_json TEXT NOT NULL DEFAULT '["approve","reject","request_changes"]',
    payload_ref TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    resolved_by TEXT NOT NULL DEFAULT '',
    resolved_via TEXT NOT NULL DEFAULT '',
    resolution_note TEXT NOT NULL DEFAULT '',
    expires_at_epoch REAL,
    resolved_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_interaction_actions_conversation
    ON interaction_actions(tenant_id, conversation_id, status);

CREATE TABLE IF NOT EXISTS interaction_requests (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES interaction_conversations(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'message',
    text TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    dispatched_to TEXT NOT NULL DEFAULT '',
    response_message_id TEXT,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_interaction_requests_status
    ON interaction_requests(tenant_id, status, created_at);

CREATE TABLE IF NOT EXISTS interaction_responses (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    request_id TEXT REFERENCES interaction_requests(id) ON DELETE SET NULL,
    conversation_id TEXT NOT NULL REFERENCES interaction_conversations(id) ON DELETE CASCADE,
    actor TEXT NOT NULL DEFAULT 'system',
    text TEXT NOT NULL DEFAULT '',
    media_json TEXT NOT NULL DEFAULT '[]',
    delivery_status TEXT NOT NULL DEFAULT 'queued',
    detail TEXT NOT NULL DEFAULT '',
    sent_external_message_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_interaction_responses_conversation
    ON interaction_responses(conversation_id, created_at);

CREATE TABLE IF NOT EXISTS notification_routes (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    event_class TEXT NOT NULL,
    intent TEXT NOT NULL,
    channels_json TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
    FOREIGN KEY (project_id) REFERENCES tenant_projects(project_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_notification_routes_class
    ON notification_routes(tenant_id, project_id, event_class);

CREATE TABLE IF NOT EXISTS notification_deliveries (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    notification_key TEXT NOT NULL DEFAULT '',
    event_class TEXT NOT NULL DEFAULT '',
    intent TEXT NOT NULL DEFAULT 'IMMEDIATE',
    channel_type TEXT NOT NULL DEFAULT '',
    target TEXT NOT NULL DEFAULT '',
    conversation_id TEXT,
    status TEXT NOT NULL DEFAULT 'sent',
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_notification_deliveries_scope
    ON notification_deliveries(tenant_id, project_id, event_class, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_notification_deliveries_dedupe
    ON notification_deliveries(tenant_id, notification_key, channel_type, target)
    WHERE notification_key <> '';
"""


def init_interaction_schema(db: sqlite3.Connection) -> None:
    db.executescript(_DDL)
    # Forward-compatible column additions live here so older databases upgrade
    # in place exactly like the rest of the app's schema modules.
    _add_columns(
        db,
        "interaction_connections",
        {
            "verified_at": "TEXT",
            # Empty preserves automatic selection for connections created
            # before the explicit Telegram-agent setting existed.
            "operator_profile_id": "TEXT NOT NULL DEFAULT ''",
        },
    )
