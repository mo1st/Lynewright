from __future__ import annotations

import json
import sqlite3

import pytest

from app.services.content_review import (
    CONTENT_REVIEW_INTEGRATION_ID,
    ContentReviewService,
    REJECTION_TAXONOMY,
    install_content_render_gate,
)
from app.services.integration_registry import IntegrationRegistry
from app.services.platform_services import (
    JobService,
    PlatformOperationError,
    PlatformStore,
    reset_job_creation_gates,
)


def _service(db: sqlite3.Connection) -> ContentReviewService:
    store = PlatformStore(lambda: db)
    return ContentReviewService(store)


def _content(service: ContentReviewService, **overrides: object) -> dict[str, object]:
    payload = {
        "topic_id": "topic_skin_017",
        "market": "MA",
        "language": "ar",
        "register": "darija",
        "target_platform": "instagram",
        "hook": "Three steps most people skip before choosing a product.",
        "script": "A sourced explanation of a routine, with one clear next step.",
        "sources": [{"title": "Example review", "url": "https://example.test/source"}],
    }
    payload.update(overrides)
    return service.create_content(payload)


def _approve_all(service: ContentReviewService, content_id: str) -> None:
    for gate in ("integrity", "accuracy", "persuasion"):
        service.record_human_verdict(
            content_id, gate=gate, status="APPROVED", reviewer_id="reviewer-1"
        )


def test_gate_record_persists_verdicts_reviewer_type_and_script_version() -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False); db.row_factory = sqlite3.Row
    service = _service(db)
    created = _content(service)
    service.record_human_verdict(
        created["content_id"], gate="integrity", status="APPROVED",
        reviewer_id="reviewer-1", reviewer_type="ai_assisted_human",
    )
    service.record_human_verdict(
        created["content_id"], gate="accuracy", status="APPROVED", reviewer_id="reviewer-1"
    )
    record = service.gate_record(created["content_id"])

    assert record["script_version"] == created["script_version"] == 1
    assert record["integrity_status"] == "APPROVED"
    assert record["accuracy_status"] == "APPROVED"
    assert record["persuasion_status"] == "PENDING"
    integrity = next(v for v in record["verdicts"] if v["gate"] == "integrity")
    assert integrity["reviewed_by_type"] == "ai_assisted_human"
    assert integrity["reviewer_id"] == "reviewer-1"
    assert integrity["script_version"] == 1
    db.close()


def test_render_gate_blocks_when_any_verdict_is_not_approved() -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False); db.row_factory = sqlite3.Row
    service = _service(db)
    created = _content(service)

    with pytest.raises(PlatformOperationError, match="render"):
        service.authorize_render(created["content_id"], script_version=1)

    service.record_human_verdict(created["content_id"], gate="integrity", status="APPROVED", reviewer_id="r")
    service.record_human_verdict(created["content_id"], gate="accuracy", status="APPROVED", reviewer_id="r")
    service.record_human_verdict(
        created["content_id"], gate="persuasion", status="REJECTED",
        reviewer_id="r", rejection_reasons=["WEAK_HOOK"],
    )
    with pytest.raises(PlatformOperationError, match="render"):
        service.authorize_render(created["content_id"], script_version=1)
    db.close()


def test_render_gate_blocks_when_reviewed_script_version_differs_from_current() -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False); db.row_factory = sqlite3.Row
    service = _service(db)
    created = _content(service)
    _approve_all(service, created["content_id"])
    updated = service.update_script(
        created["content_id"],
        hook="A sharper hook.",
        script="A revised script body.",
        sources=[{"title": "New source", "url": "https://example.test/v2"}],
    )
    assert updated["script_version"] == 2

    with pytest.raises(PlatformOperationError):
        service.authorize_render(created["content_id"], script_version=updated["script_version"])
    with pytest.raises(PlatformOperationError):
        service.authorize_render(created["content_id"], script_version=created["script_version"])
    record = service.gate_record(created["content_id"])
    assert record["integrity_status"] == "STALE"
    assert record["accuracy_status"] == "STALE"
    assert record["persuasion_status"] == "STALE"
    db.close()


def test_render_gate_requires_human_verified_evidence() -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False); db.row_factory = sqlite3.Row
    service = _service(db)
    created = _content(service)
    _approve_all(service, created["content_id"])
    assert service.gate_record(created["content_id"])["review_evidence_state"] == "VERIFIED"

    manifest = service.registry.get(CONTENT_REVIEW_INTEGRATION_ID)["manifest"]
    manifest["name"] = "Reviewer definition changed"
    db.execute(
        "UPDATE platform_extensions SET manifest_json=? WHERE tenant_id=? AND id=?",
        (json.dumps(manifest), "ten_default", CONTENT_REVIEW_INTEGRATION_ID),
    )
    db.commit()

    record = service.gate_record(created["content_id"])
    assert record["integrity_status"] == "APPROVED"
    assert record["review_evidence_state"] != "VERIFIED"
    with pytest.raises(Exception):
        service.authorize_render(created["content_id"], script_version=1)
    db.close()


def test_ai_precheck_records_declared_evidence_only() -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False); db.row_factory = sqlite3.Row
    service = _service(db)
    created = _content(service)
    result = service.record_ai_precheck(
        created["content_id"],
        detected_risks=["possible therapeutic wording in beat 2"],
        suggested_sources=[{"title": "Guideline", "url": "https://example.test/guide"}],
    )
    assert result["evidence"]["state"] == "DECLARED"
    assert result["evidence"]["verified"] is False
    assert result["authoritative"] is False

    record = service.gate_record(created["content_id"])
    assert record["review_evidence_state"] == "DECLARED"
    with pytest.raises(Exception):
        service.authorize_render(created["content_id"], script_version=1)
    db.close()


def test_ai_precheck_cannot_promote_itself_to_verified() -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False); db.row_factory = sqlite3.Row
    service = _service(db)
    created = _content(service)
    service.record_ai_precheck(created["content_id"])
    service.record_ai_precheck(created["content_id"], detected_risks=[])

    evidence = service.evidence.get(
        integration_id=CONTENT_REVIEW_INTEGRATION_ID,
        contract=service.CONTRACT,
        capability=service.CAPABILITY,
    )
    assert evidence["state"] == "DECLARED" and evidence["verified"] is False

    promoted = service.evidence.set_state(
        integration_id=CONTENT_REVIEW_INTEGRATION_ID,
        contract=service.CONTRACT,
        capability=service.CAPABILITY,
        state="DISCOVERED",
        reason="pre-check discovered sources",
    )
    assert promoted["state"] == "DISCOVERED" and promoted["verified"] is False
    with pytest.raises(Exception):
        service.evidence.require_usable(
            integration_id=CONTENT_REVIEW_INTEGRATION_ID,
            contract=service.CONTRACT,
            capability=service.CAPABILITY,
            require_verified=True,
        )
    db.close()


def test_render_gate_is_enforced_in_the_job_creation_path() -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False); db.row_factory = sqlite3.Row
    service = _service(db)
    created = _content(service)
    install_content_render_gate(store=PlatformStore(lambda: db))
    try:
        jobs = JobService(PlatformStore(lambda: db))
        with pytest.raises(PlatformOperationError):
            jobs.create({"type": "video.render", "payload": {
                "content_id": created["content_id"], "script_version": 1,
            }})
        assert jobs.list() == []

        _approve_all(service, created["content_id"])
        job = jobs.create({"type": "video.render", "payload": {
            "content_id": created["content_id"], "script_version": 1,
        }})
        assert job["status"] == "queued"
    finally:
        reset_job_creation_gates()
    db.close()


def test_render_gate_cannot_be_bypassed_by_calling_job_create_directly() -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False); db.row_factory = sqlite3.Row
    service = _service(db)
    created = _content(service)
    install_content_render_gate(store=PlatformStore(lambda: db))
    try:
        fresh_jobs = JobService()
        with pytest.raises(PlatformOperationError):
            fresh_jobs.create({"type": "video.render", "payload": {
                "content_id": created["content_id"], "script_version": 1,
            }})
        unrelated = fresh_jobs.create({"type": "email.send", "payload": {"to": "customer@example.test"}})
        assert unrelated["status"] == "queued"
    finally:
        reset_job_creation_gates()
    db.close()


def test_rejection_reason_is_recorded_with_a_taxonomy_code() -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False); db.row_factory = sqlite3.Row
    service = _service(db)
    created = _content(service)
    assert "CLAIM_THERAPEUTIC" in REJECTION_TAXONOMY

    verdict = service.record_human_verdict(
        created["content_id"], gate="integrity", status="REJECTED",
        reviewer_id="reviewer-2", rejection_reasons=["CLAIM_THERAPEUTIC", "URGENCY_FALSE"],
        notes="Beat 2 promises a cure.",
    )
    assert verdict["rejection_reasons"] == ["CLAIM_THERAPEUTIC", "URGENCY_FALSE"]

    with pytest.raises(PlatformOperationError):
        service.record_human_verdict(
            created["content_id"], gate="accuracy", status="REJECTED",
            reviewer_id="reviewer-2", rejection_reasons=["NOT_A_REAL_CODE"],
        )
    with pytest.raises(PlatformOperationError):
        service.record_human_verdict(
            created["content_id"], gate="accuracy", status="REJECTED",
            reviewer_id="reviewer-2", rejection_reasons=[],
        )
    record = service.gate_record(created["content_id"])
    integrity = next(v for v in record["verdicts"] if v["gate"] == "integrity")
    assert integrity["status"] == "REJECTED"
    db.close()
