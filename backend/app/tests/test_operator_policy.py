"""Session G tests: risk classification, bounded settings, forbidden changes."""
from __future__ import annotations

import sqlite3

import pytest

from app.services.operator_policy import (
    HARD_APPROVAL_CLASSES,
    RISK_CLASSES,
    ActionDescriptor,
    OperatorActionRegistry,
    OperatorPolicyEngine,
    build_default_registry,
    default_decision_for_risk,
)
from app.services.operator_settings_bounds import BoundedSettingRegistry
from app.services.platform_services import PlatformStore, PlatformValidationError


CAPS = {"jobs:read", "jobs:write", "system:read", "system:diagnostics", "settings.write", "ai.test", "ai.read"}
FULL_CAPS = CAPS | {"credential.manage", "admin"}


@pytest.fixture()
def engine():
    return OperatorPolicyEngine(build_default_registry())


@pytest.fixture()
def registry():
    registry = BoundedSettingRegistry()
    registry.register(key="platform.render.timeout_seconds", value_type="integer",
                      minimum=60, maximum=600, verification="renderer_health_and_test_job",
                      description="Render timeout", unit="seconds")
    registry.register(key="platform.copy.tone", value_type="enum",
                      choices=["neutral", "playful"], verification="copy_review")
    return registry


# ------------------------------------------------------------ shared vocabulary


def test_shared_risk_vocabulary_is_exact() -> None:
    assert RISK_CLASSES == (
        "READ_ONLY", "SAFE_OPERATION", "BOUNDED_CONFIG", "PUBLICATION",
        "MONEY_MOVING", "DESTRUCTIVE", "SECURITY_SENSITIVE",
    )
    assert HARD_APPROVAL_CLASSES == frozenset({"MONEY_MOVING", "DESTRUCTIVE", "SECURITY_SENSITIVE"})


def test_registry_rejects_invented_risk_classes_and_unsafe_registrations() -> None:
    registry = OperatorActionRegistry()
    with pytest.raises(PlatformValidationError):
        registry.register(ActionDescriptor(name="x", risk_class="LOW_RISK", required_capability="system:read"))
    with pytest.raises(PlatformValidationError):
        registry.register(ActionDescriptor(
            name="y", risk_class="MONEY_MOVING", required_capability="admin",
            approval_policy="automatic",
        ))
    with pytest.raises(PlatformValidationError):
        registry.register(ActionDescriptor(name="z", risk_class="BOUNDED_CONFIG",
                                          required_capability="settings.write", bounds=None))


# ---------------------------------------------------------- default decisions


def test_read_only_allowed_when_capable_forbidden_when_not(engine) -> None:
    decision = engine.decide("system.inspect_health", actor_capabilities=CAPS)
    assert decision.decision == "allow"
    denied = engine.decide("system.inspect_health", actor_capabilities=set())
    assert denied.decision == "forbidden" and "missing_capability" in denied.reasons[0]


def test_safe_operation_automatic_only_if_allowlisted_reversible_or_idempotent() -> None:
    descriptor = ActionDescriptor(
        name="custom.safe_op", risk_class="SAFE_OPERATION", required_capability="jobs:write",
        reversible=True, idempotent=True, approval_policy="automatic",
    )
    assert default_decision_for_risk(descriptor, capabilities=CAPS).decision == "allow"
    # Not allowlisted (default approval policy) -> approval.
    cautious = ActionDescriptor(
        name="custom.cautious", risk_class="SAFE_OPERATION", required_capability="jobs:write",
        reversible=True, idempotent=True,
    )
    assert default_decision_for_risk(cautious, capabilities=CAPS).decision == "approval_required"
    # Allowlisted but neither reversible nor idempotent -> approval.
    fragile = ActionDescriptor(
        name="custom.fragile", risk_class="SAFE_OPERATION", required_capability="jobs:write",
        reversible=False, idempotent=False, approval_policy="automatic",
    )
    assert default_decision_for_risk(fragile, capabilities=CAPS).decision == "approval_required"


def test_hard_approval_classes_always_require_approval_even_with_full_caps(engine) -> None:
    for name in ("builder.apply_change_set",):  # DESTRUCTIVE by registration
        decision = engine.decide(name, actor_capabilities=FULL_CAPS)
        assert decision.decision == "approval_required"
    for action_name in ("money.transfer_funds", "store.delete_all_products"):
        engine.registry.register(ActionDescriptor(
            name=action_name, risk_class="MONEY_MOVING" if "transfer" in action_name else "DESTRUCTIVE",
            required_capability="admin", approval_policy="approval_required",
        ))
        assert engine.decide(action_name, actor_capabilities=FULL_CAPS).decision == "approval_required"


def test_security_sensitive_never_via_untrusted_channel_identity(engine) -> None:
    engine.registry.register(ActionDescriptor(
        name="security.rotate_policy", risk_class="SECURITY_SENSITIVE", required_capability="admin",
    ))
    decision = engine.decide("security.rotate_policy", actor_capabilities=FULL_CAPS)
    assert decision.decision == "approval_required"
    channel = engine.decide("security.rotate_policy", actor_capabilities=FULL_CAPS,
                            context={"untrusted_channel": True})
    assert channel.decision == "approval_required"
    assert "untrusted_channel" in channel.reasons[0]


def test_unknown_action_denied_by_default(engine) -> None:
    decision = engine.decide("totally.unknown.action", actor_capabilities=FULL_CAPS)
    assert decision.decision == "forbidden"
    assert "unknown_actions_are_denied_by_default" in decision.reasons


def test_publication_defaults_to_approval_but_is_configurable(engine) -> None:
    engine.registry.register(ActionDescriptor(
        name="publication.publish_page", risk_class="PUBLICATION", required_capability="admin",
        requires_verification=True,
    ))
    assert engine.decide("publication.publish_page", actor_capabilities=FULL_CAPS).decision == "approval_required"
    outcome = engine.set_override("publication.publish_page", "automatic", reason="merchant opted in")
    assert outcome.decision == "allow"
    assert engine.decide("publication.publish_page", actor_capabilities=FULL_CAPS).decision == "allow"
    # Tightening is always allowed too.
    engine.set_override("publication.publish_page", "forbidden")
    assert engine.decide("publication.publish_page", actor_capabilities=FULL_CAPS).decision == "forbidden"


def test_overrides_cannot_relax_hard_approval_classes(engine) -> None:
    result = engine.set_override("builder.apply_change_set", "automatic")
    assert result.decision != "allow"
    still = engine.decide("builder.apply_change_set", actor_capabilities=FULL_CAPS)
    assert still.decision == "approval_required"


def test_raw_secret_access_is_registered_as_outright_forbidden(engine) -> None:
    decision = engine.decide("credentials.reveal_secret", actor_capabilities=FULL_CAPS)
    assert decision.decision == "forbidden"
    assert engine.decide("settings.unbounded_write", actor_capabilities=FULL_CAPS).decision == "forbidden"


# ------------------------------------------------------------ bounded settings


def test_unknown_setting_cannot_be_auto_modified(registry) -> None:
    ok, reason = registry.validate_value("platform.video.some_random_knob", 120)
    assert ok is False and reason == "setting_not_registered_for_operator_write"


def test_bounded_setting_inside_range_passes_and_normalizes(registry) -> None:
    ok, reason = registry.validate_value("platform.render.timeout_seconds", 300)
    assert ok is True
    assert registry.normalize("platform.render.timeout_seconds", "120") == 120


def test_bounded_setting_out_of_range_rejected_before_write(registry) -> None:
    for bad, reason_part in ((59, "below_minimum"), (601, "above_maximum"), ("abc", "numeric"), (99.5, "integer")):
        ok, reason = registry.validate_value("platform.render.timeout_seconds", bad)
        assert ok is False and reason_part in reason


def test_enum_setting_rejects_unlisted_choices(registry) -> None:
    ok, _ = registry.validate_value("platform.copy.tone", "playful")
    assert ok is True
    ok, reason = registry.validate_value("platform.copy.tone", "sarcastic")
    assert ok is False and reason == "value_not_in_allowed_choices"


def test_registry_registration_validates_metadata(registry) -> None:
    with pytest.raises(PlatformValidationError):
        registry.register(key="not registered!", value_type="number")
    with pytest.raises(PlatformValidationError):
        registry.register(key="platform.x.y", value_type="quantum")
    with pytest.raises(PlatformValidationError):
        registry.register(key="platform.x.y", value_type="number", minimum=10, maximum=5)
    with pytest.raises(PlatformValidationError):
        registry.register(key="platform.x.y", value_type="enum")


# ------------------------------------------------------- persisted overrides


def test_overrides_survive_process_reconstruction() -> None:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db)
    first = OperatorPolicyEngine(build_default_registry(), store=store)
    first.set_override("jobs.retry_failed", "approval_required", reason="too noisy today")
    second = OperatorPolicyEngine(build_default_registry(), store=store)
    assert second.get_override("jobs.retry_failed") == "approval_required"
    assert second.decide("jobs.retry_failed", actor_capabilities=FULL_CAPS).decision == "approval_required"
    second.clear_override("jobs.retry_failed")
    third = OperatorPolicyEngine(build_default_registry(), store=store)
    assert third.get_override("jobs.retry_failed") is None
    db.close()


def test_default_registry_explain_exposes_bounds_and_metadata(engine) -> None:
    explained = engine.explain("jobs.retry_failed")
    assert explained["risk_class"] == "SAFE_OPERATION"
    assert explained["idempotent"] is True and explained["requires_verification"] is True
    assert engine.explain("settings.bounded_update")["risk_class"] == "BOUNDED_CONFIG"
