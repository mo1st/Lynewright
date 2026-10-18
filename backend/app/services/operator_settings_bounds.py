"""Bounded operator-writable settings registry (Session G).

``SettingsService.set`` accepts generic values — handing that to an agent is an
unconstrained self-healing primitive. Only settings explicitly registered here
may be changed automatically, and only inside their declared bounds. Unknown
settings are rejected before any write happens.

Metadata follows the master plan's shape:

    {
      "key": "video.render_timeout_seconds",
      "risk": "BOUNDED_CONFIG",
      "operator_write": true,
      "min": 60, "max": 600,
      "verification": "renderer_health_and_test_job"
    }

Bounds metadata lives in code/configuration on purpose: static limits need no
persistence, and the registry must be auditable by reading one module. The
registry itself never writes; the remediation service performs the
plan → policy → apply → verify → rollback flow around it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from app.services.platform_services import PlatformValidationError


SETTING_KEY_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
VALUE_TYPES: tuple[str, ...] = ("number", "integer", "string", "boolean", "enum")

# Verification verifier callables registered by name so plans can reference a
# strategy without holding executable objects in persisted JSON.
VerifierFactory = Callable[[Any], "SettingVerifier"]


@dataclass(frozen=True)
class BoundedSetting:
    key: str
    value_type: str  # number | integer | string | boolean | enum
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] | None = None
    max_length: int | None = None
    verification: str = ""
    description: str = ""
    unit: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value_type": self.value_type,
            "operator_write": True,
            "risk": "BOUNDED_CONFIG",
            "minimum": self.minimum,
            "maximum": self.maximum,
            "choices": list(self.choices) if self.choices else None,
            "max_length": self.max_length,
            "verification": self.verification,
            "description": self.description,
            "unit": self.unit,
        }


class BoundedSettingRegistry:
    """Code-owned metadata for every setting an agent may change."""

    def __init__(self) -> None:
        self._settings: dict[str, BoundedSetting] = {}

    def register(
        self,
        *,
        key: str,
        value_type: str = "number",
        minimum: float | None = None,
        maximum: float | None = None,
        choices: list[str] | tuple[str, ...] | None = None,
        max_length: int | None = None,
        verification: str = "",
        description: str = "",
        unit: str = "",
    ) -> BoundedSetting:
        if not SETTING_KEY_RE.fullmatch(str(key or "")):
            raise PlatformValidationError("Setting keys must look like 'namespace.name' and stay under 128 characters.")
        if value_type not in VALUE_TYPES:
            raise PlatformValidationError(f"value_type must be one of: {', '.join(VALUE_TYPES)}.")
        if minimum is not None and maximum is not None and float(minimum) > float(maximum):
            raise PlatformValidationError("Bounded setting minimum cannot exceed maximum.")
        normalized_choices = tuple(str(item) for item in choices) if choices else None
        if value_type == "enum" and not normalized_choices:
            raise PlatformValidationError("Enum settings require choices.")
        entry = BoundedSetting(
            key=str(key), value_type=value_type,
            minimum=None if minimum is None else float(minimum),
            maximum=None if maximum is None else float(maximum),
            choices=normalized_choices, max_length=max_length,
            verification=str(verification)[:120], description=str(description)[:500],
            unit=str(unit)[:24],
        )
        self._settings[entry.key] = entry
        return entry

    def get(self, key: str) -> BoundedSetting | None:
        return self._settings.get(str(key))

    def is_operator_writable(self, key: str) -> bool:
        return str(key) in self._settings

    def list(self) -> list[BoundedSetting]:
        return sorted(self._settings.values(), key=lambda item: item.key)

    def validate_value(self, key: str, value: Any) -> tuple[bool, str]:
        """Bounds check BEFORE any write. Returns (ok, reason)."""
        entry = self._settings.get(str(key))
        if entry is None:
            return False, "setting_not_registered_for_operator_write"
        if entry.value_type == "boolean":
            if not isinstance(value, bool):
                return False, "value_must_be_boolean"
            return True, ""
        if entry.value_type == "number" or entry.value_type == "integer":
            number = _coerce_number(value)
            if number is None:
                return False, "value_must_be_numeric"
            if entry.value_type == "integer" and not float(number).is_integer():
                return False, "value_must_be_an_integer"
            if entry.minimum is not None and number < entry.minimum:
                return False, f"value_below_minimum_{_fmt(entry.minimum)}"
            if entry.maximum is not None and number > entry.maximum:
                return False, f"value_above_maximum_{_fmt(entry.maximum)}"
            return True, ""
        if entry.value_type == "enum":
            text = str(value)
            if entry.choices is not None and text not in entry.choices:
                return False, "value_not_in_allowed_choices"
            return True, ""
        text = "" if value is None else str(value)
        limit = entry.max_length if entry.max_length is not None else 2_000
        if len(text) > limit:
            return False, f"value_exceeds_max_length_{limit}"
        return True, ""

    def normalize(self, key: str, value: Any) -> Any:
        entry = self._settings.get(str(key))
        if entry is None:
            return value
        if entry.value_type in {"number", "integer"}:
            number = _coerce_number(value)
            if number is None:
                return value
            return int(number) if entry.value_type == "integer" else number
        return value


def _coerce_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt(number: float) -> str:
    return str(int(number)) if float(number).is_integer() else str(number)


# Module-level registry instance: registration happens at import/config time so
# every process agrees on what is operator-writable.
BOUNDED_SETTINGS = BoundedSettingRegistry()


def _register_operator_operational_knobs(registry: BoundedSettingRegistry) -> None:
    """The operator knobs an agent may tune, with their envelopes.

    These are the only settings that are *both* operator-writable and actually
    read at runtime: :mod:`app.services.operator_runtime_overrides` resolves each
    one over its environment default, so a bounded change here really takes
    effect instead of writing to a key nothing reads.

    Deliberately absent: ``operator_automation_enabled`` and
    ``operator_dispatch_enabled``. An agent must not be able to switch its own
    execution or oversight on or off — that is an operator-lifecycle decision,
    not a bounded tuning knob.
    """
    registry.register(
        key="platform.operator.automation_tick_seconds",
        value_type="integer", minimum=5, maximum=3600,
        verification="next_tick_cadence",
        description="Seconds between hosted operator automation ticks.",
        unit="seconds",
    )
    registry.register(
        key="platform.operator.objectives_per_tick",
        value_type="integer", minimum=1, maximum=100,
        verification="dispatch_batch_observed",
        description="Maximum operator objective requests drained per tick.",
        unit="requests",
    )
    registry.register(
        key="platform.operator.interactions_per_tick",
        value_type="integer", minimum=1, maximum=200,
        verification="dispatch_batch_observed",
        description="Maximum inbound interaction requests routed per tick.",
        unit="requests",
    )
    registry.register(
        key="platform.operator.max_tenants_per_tick",
        value_type="integer", minimum=1, maximum=2000,
        verification="tenant_fanout_observed",
        description="Maximum tenants visited in one hosted operator tick.",
        unit="tenants",
    )
    registry.register(
        key="platform.operator.remediation_max_attempts",
        value_type="integer", minimum=1, maximum=10,
        verification="remediation_attempt_budget",
        description="Attempts allowed per remediation correlation before escalating.",
        unit="attempts",
    )


_register_operator_operational_knobs(BOUNDED_SETTINGS)


__all__ = [
    "BOUNDED_SETTINGS",
    "BoundedSetting",
    "BoundedSettingRegistry",
]
