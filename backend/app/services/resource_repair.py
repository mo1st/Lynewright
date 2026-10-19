"""Conservative policy for deciding whether provider drift may self-repair."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RepairDecision:
    allowed: bool
    code: str
    reason: str


class ResourceRepairPolicy:
    """Keep automatic repair narrow; ambiguous drift remains operator-owned."""

    MAX_AUTOMATIC_REPAIRS = 3

    @staticmethod
    def decide(*, binding: dict[str, Any], provider: Any, observed_fingerprint: str) -> RepairDecision:
        if binding.get("state") != "DEGRADED":
            return RepairDecision(False, "binding_not_degraded", "Only degraded bindings may be repaired.")
        if binding.get("ownership_mode") != "managed":
            return RepairDecision(False, "ownership_not_managed", "Linked or observed resources require operator confirmation.")
        desired = str(binding.get("desired_fingerprint") or "")
        if not desired or not observed_fingerprint or desired == observed_fingerprint:
            return RepairDecision(False, "drift_not_unambiguous", "A bounded desired/observed fingerprint mismatch is required.")
        metadata = binding.get("metadata") if isinstance(binding.get("metadata"), dict) else {}
        try:
            repair_count = int(metadata.get("automatic_repair_count", 0))
        except (TypeError, ValueError):
            return RepairDecision(False, "repair_count_invalid", "Automatic repair history is invalid and requires operator review.")
        if repair_count < 0 or repair_count >= ResourceRepairPolicy.MAX_AUTOMATIC_REPAIRS:
            return RepairDecision(False, "repair_limit_reached", "Automatic repair is capped; operator review is required.")
        if not callable(getattr(provider, "repair", None)):
            return RepairDecision(False, "provider_repair_unsupported", "The provider has not opted into automatic repair.")
        return RepairDecision(True, "safe_managed_drift", "Managed fingerprint drift can be repaired idempotently.")


__all__ = ["RepairDecision", "ResourceRepairPolicy"]
