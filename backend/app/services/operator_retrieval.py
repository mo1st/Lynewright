"""Durable, tenant-scoped hybrid retrieval for Operator evidence.

The index is deliberately an application-owned SQLite sidecar rather than an
integration-specific cache.  Every current and future integration can expose
normal Lynewright evidence cards through the same schema; exact lexical lookup
works immediately and a compact local multilingual vector supplies a bounded
semantic rerank without a per-message provider call.  Authoritative records
and secrets remain in their source systems -- this index holds only safe
display/search fields and typed navigation references.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from typing import Any, Callable, Iterable

from app.services.operator_runtime.context import normalize_text, search_terms, stable_digest


INDEX_REVISION = "lynewright-local-hybrid-v1"
VECTOR_DIMENSIONS = 96
MAX_SCOPE_DOCUMENTS = 500
_SOURCE_CARD_PROVIDERS: list[Callable[..., list[dict[str, str]]]] = []


def register_source_card_provider(provider: Callable[..., list[dict[str, str]]]) -> Callable[[], bool]:
    """Register optional, tenant-scoped evidence card sources."""
    if not callable(provider):
        raise TypeError("Source card provider must be callable.")
    _SOURCE_CARD_PROVIDERS.append(provider)

    def unregister() -> bool:
        if provider not in _SOURCE_CARD_PROVIDERS:
            return False
        _SOURCE_CARD_PROVIDERS.remove(provider)
        return True

    return unregister

# Concept labels deliberately describe generic business actions, never a
# provider.  They improve Arabic/English cross-language discovery while a
# future configured embedding integration may replace the local scorer.
_CONCEPTS: dict[str, tuple[str, ...]] = {
    "create": ("create", "generate", "make", "build", "أنشئ", "انشئ", "إنشاء", "انشاء", "إنشاء"),
    "publish": ("publish", "post", "share", "نشر", "انشر", "منشور"),
    "video": ("video", "clip", "short", "فيديو", "فديو", "مقطع"),
    "research": ("research", "trend", "discover", "بحث", "ترند", "اكتشف"),
    "analysis": ("analyse", "analyze", "analysis", "report", "تحليل", "حلل", "تقرير"),
    "message": ("message", "telegram", "send", "رسالة", "تيليجرام", "أرسل", "ارسل"),
}


def init_operator_retrieval_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS operator_retrieval_documents (
            document_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            source_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            subtitle TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            destination_url TEXT NOT NULL DEFAULT '',
            search_text TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            content_hash TEXT NOT NULL,
            source_revision TEXT NOT NULL DEFAULT '',
            authorization_revision TEXT NOT NULL DEFAULT '',
            embedding_revision TEXT NOT NULL DEFAULT '',
            embedding_json TEXT NOT NULL DEFAULT '[]',
            index_state TEXT NOT NULL DEFAULT 'ready',
            observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(tenant_id, project_id, source_kind, source_id)
        );
        CREATE INDEX IF NOT EXISTS idx_operator_retrieval_scope
            ON operator_retrieval_documents(tenant_id, project_id, source_kind, updated_at DESC);
        """
    )
    # FTS5 is compiled into the supported SQLite images.  Keep a normal-table
    # fallback, though, so a restricted downstream image keeps safe lexical
    # retrieval rather than failing a Telegram request at runtime.
    try:
        db.execute(
            """CREATE VIRTUAL TABLE IF NOT EXISTS operator_retrieval_fts
               USING fts5(document_id UNINDEXED, tenant_id UNINDEXED,
                          project_id UNINDEXED, source_kind UNINDEXED,
                          title, search_text,
                          tokenize='unicode61 remove_diacritics 2')"""
        )
    except sqlite3.OperationalError:
        pass


def _fts_available(db: sqlite3.Connection) -> bool:
    try:
        row = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operator_retrieval_fts'"
        ).fetchone()
        return row is not None
    except sqlite3.OperationalError:
        return False


def _concept_tokens(value: Any) -> set[str]:
    terms = set(search_terms(value))
    concepts: set[str] = set()
    for concept, aliases in _CONCEPTS.items():
        if terms.intersection(search_terms(" ".join(aliases))):
            concepts.add(f"concept:{concept}")
    return concepts


def _vector(value: Any) -> list[float]:
    """Small stable feature-hash embedding for zero-cost multilingual rerank."""
    weights = [0.0] * VECTOR_DIMENSIONS
    tokens = list(search_terms(value)) + sorted(_concept_tokens(value))
    normalized = normalize_text(value)
    # Word/character features make Arabic, typos and compact identifiers useful
    # even when a source did not provide a translated title.
    features = tokens[:80]
    compact = normalized.replace(" ", "")
    features.extend(compact[index:index + 3] for index in range(max(0, len(compact) - 2))[:120])
    for feature in features:
        if not feature:
            continue
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        bucket = int.from_bytes(digest[:4], "big") % VECTOR_DIMENSIONS
        sign = 1.0 if digest[4] & 1 else -1.0
        weights[bucket] += sign
    magnitude = math.sqrt(sum(weight * weight for weight in weights))
    return [round(weight / magnitude, 6) for weight in weights] if magnitude else weights


def _cosine(left: Iterable[float], right: Iterable[float]) -> float:
    return max(0.0, min(1.0, sum(a * b for a, b in zip(left, right))))


def _source_cards(db: sqlite3.Connection, *, tenant_id: str, project_id: str) -> list[dict[str, str]]:
    """Read optional evidence cards registered by an active integration."""
    cards: list[dict[str, str]] = []
    for provider in tuple(_SOURCE_CARD_PROVIDERS):
        cards.extend(provider(db, tenant_id=tenant_id, project_id=project_id))
    return cards


def sync_project_evidence(db: sqlite3.Connection, *, tenant_id: str, project_id: str) -> dict[str, int | bool]:
    """Synchronize current source cards into the durable hybrid index.

    Called at bounded query time, so updates/revocations become visible on the
    next authorized search even when a background worker is paused.  The same
    function is safe to call from a future durable indexing job.
    """
    init_operator_retrieval_schema(db)
    cards = _source_cards(db, tenant_id=tenant_id, project_id=project_id)
    desired = {card["id"] for card in cards}
    existing = {
        str(row["document_id"]): str(row["content_hash"])
        for row in db.execute(
            "SELECT document_id,content_hash FROM operator_retrieval_documents WHERE tenant_id=? AND project_id=?",
            (tenant_id, project_id),
        ).fetchall()
    }
    fts = _fts_available(db)
    changed = 0
    for card in cards:
        content = {
            "title": card["title"], "subtitle": card["subtitle"], "status": card["status"],
            "kind": card["source_kind"],
        }
        digest = stable_digest(content)
        if existing.get(card["id"]) == digest:
            continue
        vector = _vector(" ".join((card["title"], card["subtitle"], card["status"], card["source_kind"])))
        db.execute(
            """INSERT INTO operator_retrieval_documents
               (document_id,tenant_id,project_id,source_kind,source_id,title,subtitle,status,destination_url,
                search_text,metadata_json,content_hash,source_revision,authorization_revision,embedding_revision,embedding_json,index_state)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(document_id) DO UPDATE SET title=excluded.title,subtitle=excluded.subtitle,
                 status=excluded.status,destination_url=excluded.destination_url,search_text=excluded.search_text,
                 metadata_json=excluded.metadata_json,content_hash=excluded.content_hash,
                 source_revision=excluded.source_revision,authorization_revision=excluded.authorization_revision,
                 embedding_revision=excluded.embedding_revision,embedding_json=excluded.embedding_json,
                 index_state='ready',observed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP""",
            (
                card["id"], tenant_id, project_id, card["source_kind"], card["source_id"], card["title"],
                card["subtitle"], card["status"], card["destination_url"],
                " ".join((card["title"], card["subtitle"], card["status"], card["source_kind"])),
                json.dumps({"result_id": card["id"]}, ensure_ascii=False), digest, digest, "tenant-project-v1",
                INDEX_REVISION, json.dumps(vector, separators=(",", ":")), "ready",
            ),
        )
        if fts:
            db.execute("DELETE FROM operator_retrieval_fts WHERE document_id=?", (card["id"],))
            db.execute(
                "INSERT INTO operator_retrieval_fts(document_id,tenant_id,project_id,source_kind,title,search_text) VALUES (?,?,?,?,?,?)",
                (card["id"], tenant_id, project_id, card["source_kind"], card["title"], " ".join((card["title"], card["subtitle"], card["status"], card["source_kind"]))),
            )
        changed += 1
    stale = sorted(set(existing) - desired)
    for document_id in stale:
        if fts:
            db.execute("DELETE FROM operator_retrieval_fts WHERE document_id=?", (document_id,))
        db.execute("DELETE FROM operator_retrieval_documents WHERE document_id=?", (document_id,))
    return {"documents": len(cards), "changed": changed, "removed": len(stale), "fts": fts}


def _lexical_ids(db: sqlite3.Connection, *, tenant_id: str, project_id: str, query: str, limit: int) -> list[tuple[str, float]]:
    terms = search_terms(query)
    if not terms:
        return []
    if _fts_available(db):
        match = " OR ".join(f'"{term.replace(chr(34), "")}"' for term in terms[:12])
        try:
            rows = db.execute(
                """SELECT document_id,bm25(operator_retrieval_fts) AS rank
                   FROM operator_retrieval_fts
                   WHERE operator_retrieval_fts MATCH ? AND tenant_id=? AND project_id=?
                   ORDER BY rank LIMIT ?""",
                (match, tenant_id, project_id, max(limit * 4, 30)),
            ).fetchall()
            return [(str(row["document_id"]), max(0.0, 1.0 / (1.0 + abs(float(row["rank"] or 0.0))))) for row in rows]
        except sqlite3.OperationalError:
            pass
    clauses = " OR ".join("search_text LIKE ? ESCAPE '\\\\'" for _ in terms[:8])
    params: list[Any] = [tenant_id, project_id]
    params.extend(f"%{term.replace('%', '\\%').replace('_', '\\_')}%" for term in terms[:8])
    rows = db.execute(
        f"SELECT document_id,search_text FROM operator_retrieval_documents WHERE tenant_id=? AND project_id=? AND ({clauses}) LIMIT ?",
        (*params, max(limit * 4, 30)),
    ).fetchall()
    return [(str(row["document_id"]), float(len(set(terms).intersection(search_terms(row["search_text"])))) ) for row in rows]


def search_project_evidence(
    db: sqlite3.Connection, *, tenant_id: str, project_id: str, query: str,
    requested_types: list[str] | None = None, limit: int = 20,
    retrieval_mode: str = "hybrid_local",
) -> dict[str, Any]:
    """Return compact, provenance-bearing cards ranked by lexical+semantic score."""
    mode = retrieval_mode if retrieval_mode in {"lexical", "hybrid_local"} else "lexical"
    sync = sync_project_evidence(db, tenant_id=tenant_id, project_id=project_id)
    kinds = {str(item) for item in (requested_types or []) if str(item)}
    lexical = dict(_lexical_ids(db, tenant_id=tenant_id, project_id=project_id, query=query, limit=limit))
    query_vector = _vector(query)
    rows = db.execute(
        "SELECT * FROM operator_retrieval_documents WHERE tenant_id=? AND project_id=? ORDER BY updated_at DESC LIMIT ?",
        (tenant_id, project_id, MAX_SCOPE_DOCUMENTS),
    ).fetchall()
    ranked: list[tuple[float, dict[str, Any]]] = []
    query_concepts = _concept_tokens(query)
    for row in rows:
        kind = str(row["source_kind"])
        if kinds and kind not in kinds:
            continue
        try:
            vector = [float(value) for value in json.loads(str(row["embedding_json"] or "[]"))]
        except (TypeError, ValueError, json.JSONDecodeError):
            vector = []
        semantic = _cosine(query_vector, vector) if mode == "hybrid_local" else 0.0
        lexical_score = lexical.get(str(row["document_id"]), 0.0)
        exact = 0.8 if normalize_text(query) and normalize_text(query) in normalize_text(row["search_text"]) else 0.0
        concept = (
            len(query_concepts.intersection(_concept_tokens(row["search_text"]))) * 0.25
            if mode == "hybrid_local" else 0.0
        )
        score = lexical_score * 2.0 + semantic + exact + concept
        if score <= 0:
            continue
        ranked.append((score, {
            "id": str(row["document_id"]), "title": str(row["title"]),
            "subtitle": str(row["subtitle"]), "status": str(row["status"]),
            "destination_url": str(row["destination_url"]), "source_kind": kind,
            "source_id": str(row["source_id"]), "score": round(score, 4),
            "source_revision": str(row["source_revision"]),
        }))
    ranked.sort(key=lambda item: (item[0], item[1]["id"]), reverse=True)
    return {
        "query": query, "project_id": project_id, "items": [item for _, item in ranked[:max(1, min(int(limit), 50))]],
        "retrieval": {"mode": mode, "index_revision": INDEX_REVISION, **sync},
    }
