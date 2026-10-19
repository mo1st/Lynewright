from __future__ import annotations

import re
import secrets
import sqlite3

DEFAULT_TENANT_ID = "ten_default"
DEFAULT_TENANT_SLUG = "default"
DEFAULT_PROJECT_ID = "demo-project"
TENANT_ROLES = ("viewer", "editor", "admin", "owner")
_TENANT_ROLE_RANK = {role: rank for rank, role in enumerate(TENANT_ROLES)}
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def tenant_role_at_least(role: str, minimum: str) -> bool:
    return _TENANT_ROLE_RANK.get(role, -1) >= _TENANT_ROLE_RANK.get(minimum, 999)


def normalize_tenant_slug(value: str) -> str:
    slug = _SLUG_RE.sub("-", value.strip().lower()).strip("-")
    return slug[:63]


def new_tenant_id() -> str:
    return f"ten_{secrets.token_hex(8)}"


def init_tenant_schema(db: sqlite3.Connection) -> None:
    """Create the ownership boundary used by all new multi-tenant modules.

    Existing single-workspace rows are attached to a deterministic default tenant
    so upgrades are non-destructive. New code must always scope by tenant_id and
    project_id together; project_id alone is never an authorization boundary.
    """
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS tenants (
            id TEXT PRIMARY KEY,
            slug TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS tenant_memberships (
            tenant_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL DEFAULT 'viewer',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (tenant_id, user_id),
            FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS tenant_projects (
            project_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
            UNIQUE (tenant_id, name)
        )
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_tenant_memberships_user ON tenant_memberships(user_id, is_active)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_tenant_projects_tenant ON tenant_projects(tenant_id, status)"
    )

    db.execute(
        """INSERT OR IGNORE INTO tenants (id, slug, name)
           VALUES (?, ?, 'Default Workspace')""",
        (DEFAULT_TENANT_ID, DEFAULT_TENANT_SLUG),
    )
    db.execute(
        """INSERT OR IGNORE INTO tenant_projects (project_id, tenant_id, name)
           VALUES (?, ?, 'Default Project')""",
        (DEFAULT_PROJECT_ID, DEFAULT_TENANT_ID),
    )

    # Existing accounts keep access to the legacy workspace after upgrade.
    db.execute(
        """
        INSERT OR IGNORE INTO tenant_memberships (tenant_id, user_id, role)
        SELECT ?, id,
               CASE role WHEN 'admin' THEN 'owner' WHEN 'editor' THEN 'editor' ELSE 'viewer' END
        FROM users
        """,
        (DEFAULT_TENANT_ID,),
    )

    session_columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)").fetchall()}
    if "active_tenant_id" not in session_columns:
        db.execute("ALTER TABLE sessions ADD COLUMN active_tenant_id TEXT")
    db.execute(
        "UPDATE sessions SET active_tenant_id = ? WHERE active_tenant_id IS NULL OR active_tenant_id = ''",
        (DEFAULT_TENANT_ID,),
    )

    # Begin migration of legacy site ownership. Other legacy modules will be
    # moved incrementally; some legacy modules are tenant-native from day one.
    site_columns = {row[1] for row in db.execute("PRAGMA table_info(sites)").fetchall()}
    if site_columns and "tenant_id" not in site_columns:
        db.execute(
            f"ALTER TABLE sites ADD COLUMN tenant_id TEXT NOT NULL DEFAULT '{DEFAULT_TENANT_ID}'"
        )
    if site_columns:
        db.execute(
            "UPDATE sites SET tenant_id = ? WHERE tenant_id IS NULL OR tenant_id = ''",
            (DEFAULT_TENANT_ID,),
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sites_tenant_project ON sites(tenant_id, project_id, id)"
        )


def ensure_user_default_membership(db: sqlite3.Connection, *, user_id: int, global_role: str) -> None:
    role = "owner" if global_role == "admin" else "editor" if global_role == "editor" else "viewer"
    db.execute(
        """INSERT OR IGNORE INTO tenant_memberships (tenant_id, user_id, role)
           VALUES (?, ?, ?)""",
        (DEFAULT_TENANT_ID, user_id, role),
    )


def default_tenant_for_user(db: sqlite3.Connection, user_id: int) -> str | None:
    row = db.execute(
        """SELECT tenant_id FROM tenant_memberships
           WHERE user_id = ? AND is_active = 1
           ORDER BY CASE role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 WHEN 'editor' THEN 2 ELSE 3 END,
                    created_at, tenant_id
           LIMIT 1""",
        (user_id,),
    ).fetchone()
    return str(row["tenant_id"]) if row else None
