from __future__ import annotations
from contextlib import closing

import secrets
import sqlite3
from typing import Any

from app.database import get_connection, init_db
from app.services.tenant_schema import (
    DEFAULT_PROJECT_ID,
    DEFAULT_TENANT_ID,
    TENANT_ROLES,
    new_tenant_id,
    normalize_tenant_slug,
)


def row_to_tenant(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def list_user_tenants(user_id: int, *, synthetic: bool = False) -> list[dict[str, Any]]:
    init_db()
    with closing(get_connection()) as db, db:
        if synthetic:
            rows = db.execute(
                """SELECT t.*, 'owner' AS role,
                          (SELECT tp.project_id FROM tenant_projects tp
                           WHERE tp.tenant_id = t.id AND tp.status = 'active'
                           ORDER BY tp.created_at, tp.project_id LIMIT 1) AS default_project_id
                   FROM tenants t
                   WHERE t.status = 'active' ORDER BY t.created_at, t.id"""
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT t.*, tm.role,
                          (SELECT tp.project_id FROM tenant_projects tp
                           WHERE tp.tenant_id = t.id AND tp.status = 'active'
                           ORDER BY tp.created_at, tp.project_id LIMIT 1) AS default_project_id
                   FROM tenants t
                   JOIN tenant_memberships tm ON tm.tenant_id = t.id
                   WHERE tm.user_id = ? AND tm.is_active = 1 AND t.status = 'active'
                   ORDER BY t.created_at, t.id""",
                (user_id,),
            ).fetchall()
    return [dict(row) for row in rows]


def create_tenant(*, name: str, slug: str, creator_user_id: int | None) -> dict[str, Any]:
    init_db()
    normalized_slug = normalize_tenant_slug(slug or name)
    if not normalized_slug:
        raise ValueError("A valid tenant name or slug is required.")
    tenant_id = new_tenant_id()
    project_id = f"prj_{secrets.token_hex(8)}"
    with closing(get_connection()) as db, db:
        try:
            db.execute(
                "INSERT INTO tenants (id, slug, name) VALUES (?, ?, ?)",
                (tenant_id, normalized_slug, name.strip()),
            )
            db.execute(
                """INSERT INTO tenant_projects (project_id, tenant_id, name)
                   VALUES (?, ?, 'Default Project')""",
                (project_id, tenant_id),
            )
            if creator_user_id and creator_user_id > 0:
                db.execute(
                    """INSERT INTO tenant_memberships (tenant_id, user_id, role)
                       VALUES (?, ?, 'owner')""",
                    (tenant_id, creator_user_id),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Tenant slug already exists.") from exc
        row = db.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    result = dict(row)
    result["role"] = "owner"
    result["default_project_id"] = project_id
    return result


def list_tenant_projects(tenant_id: str) -> list[dict[str, Any]]:
    init_db()
    with closing(get_connection()) as db, db:
        rows = db.execute(
            """SELECT * FROM tenant_projects
               WHERE tenant_id = ? AND status = 'active'
               ORDER BY created_at, project_id""",
            (tenant_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def create_tenant_project(*, tenant_id: str, name: str, project_id: str | None = None) -> dict[str, Any]:
    init_db()
    candidate = (project_id or f"prj_{secrets.token_hex(8)}").strip()
    if not candidate or len(candidate) > 100:
        raise ValueError("Invalid project id.")
    with closing(get_connection()) as db, db:
        try:
            db.execute(
                """INSERT INTO tenant_projects (project_id, tenant_id, name)
                   VALUES (?, ?, ?)""",
                (candidate, tenant_id, name.strip() or candidate),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Project id or project name already exists.") from exc
        row = db.execute(
            "SELECT * FROM tenant_projects WHERE tenant_id = ? AND project_id = ?",
            (tenant_id, candidate),
        ).fetchone()
    return dict(row)


def get_membership(*, tenant_id: str, user_id: int) -> dict[str, Any] | None:
    init_db()
    with closing(get_connection()) as db, db:
        row = db.execute(
            """SELECT tm.*, t.name AS tenant_name, t.slug AS tenant_slug
               FROM tenant_memberships tm
               JOIN tenants t ON t.id = tm.tenant_id
               WHERE tm.tenant_id = ? AND tm.user_id = ?
                 AND tm.is_active = 1 AND t.status = 'active'""",
            (tenant_id, user_id),
        ).fetchone()
    return dict(row) if row else None


def resolve_actor_role(db: sqlite3.Connection, *, tenant_id: str, user_id: int) -> str:
    """Resolve the tenant role an autonomous (scheduled/triggered/objective) run executes with.

    Autonomous paths have no request context, so the role comes from the run's creator
    rather than an assumed default. A creator who no longer holds an active membership
    drops to the least privileged role instead of inheriting elevated rights. User id 0
    is the synthetic development/break-glass admin, which already operates as owner.
    """
    if not user_id:
        return "owner"
    row = db.execute(
        """SELECT tm.role FROM tenant_memberships tm
           JOIN tenants t ON t.id = tm.tenant_id
           WHERE tm.tenant_id = ? AND tm.user_id = ?
             AND tm.is_active = 1 AND t.status = 'active'""",
        (tenant_id, user_id),
    ).fetchone()
    return str(row["role"]) if row else "viewer"


def tenant_exists(tenant_id: str) -> bool:
    init_db()
    with closing(get_connection()) as db, db:
        return db.execute(
            "SELECT 1 FROM tenants WHERE id = ? AND status = 'active'", (tenant_id,)
        ).fetchone() is not None


def require_project_in_tenant(*, tenant_id: str, project_id: str) -> dict[str, Any]:
    init_db()
    with closing(get_connection()) as db, db:
        row = db.execute(
            """SELECT * FROM tenant_projects
               WHERE tenant_id = ? AND project_id = ? AND status = 'active'""",
            (tenant_id, project_id),
        ).fetchone()
    if row is None:
        raise ValueError("Project not found in the active tenant.")
    return dict(row)


def default_project_for_tenant(tenant_id: str) -> str:
    projects = list_tenant_projects(tenant_id)
    if projects:
        return str(projects[0]["project_id"])
    if tenant_id == DEFAULT_TENANT_ID:
        return DEFAULT_PROJECT_ID
    raise ValueError("The tenant has no active project.")
