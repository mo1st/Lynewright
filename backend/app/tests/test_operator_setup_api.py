from __future__ import annotations

from app.models.operator import ResourceSetupVerificationRequest


def test_setup_verification_request_defaults_to_bounded_evidence_object() -> None:
    request = ResourceSetupVerificationRequest()
    assert request.evidence == {}
