"""Reusable conformance checks for provider-neutral integration contracts."""
from __future__ import annotations

import base64
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from app.services.extension_manifest import version_satisfies
from app.services.integration_errors import IntegrationErrorCategory, IntegrationExternalError
from app.services.platform_services import PlatformNotFoundError, PlatformOperationError
from app.services.social_publisher_contract import SOCIAL_PUBLISHER_CONTRACT, SOCIAL_PUBLISHER_VERSION

_CONFORMANCE_HARNESSES: dict[str, Callable[[Any], dict[str, Any]]] = {}


def register_conformance_harness(
    contract_id: str, harness: Callable[[Any], dict[str, Any]],
) -> Callable[[], bool]:
    """Register an optional contract check; unknown contracts stay unsupported."""
    key = str(contract_id or "").strip()
    if not key or not callable(harness):
        raise ValueError("A contract id and callable harness are required.")
    previous = _CONFORMANCE_HARNESSES.get(key)
    _CONFORMANCE_HARNESSES[key] = harness

    def unregister() -> bool:
        if _CONFORMANCE_HARNESSES.get(key) is not harness:
            return False
        if previous is None:
            _CONFORMANCE_HARNESSES.pop(key, None)
        else:
            _CONFORMANCE_HARNESSES[key] = previous
        return True

    return unregister


def run_registered_conformance(contract_id: str, provider: Any) -> dict[str, Any]:
    harness = _CONFORMANCE_HARNESSES.get(str(contract_id or "").strip())
    if harness is None:
        raise LookupError(f"No conformance harness is registered for {contract_id!r}.")
    return harness(provider)



@dataclass(frozen=True, slots=True)
class ProviderConformanceLifecycle:
    """Optional availability checks backed by the real contribution lifecycle."""

    disable: Callable[[], None]
    enable: Callable[[], None]
    restart: Callable[[], None]
    registration_count: Callable[[], int]


@dataclass(frozen=True, slots=True)
class SocialPublisherConformanceProvider:
    name: str
    invoke: Callable[[str, dict[str, Any]], dict[str, Any]]
    direct_lookup: bool
    contract_version: str = SOCIAL_PUBLISHER_VERSION
    execution_timeout_seconds: float = 30.0
    secrets: tuple[str, ...] = ()
    lifecycle: ProviderConformanceLifecycle | None = None
    supports_media_upload: bool = False


class SocialPublisherConformanceHarness:
    """Assert Social Publisher v1 behavior without provider-specific branches."""

    def run(self, provider: SocialPublisherConformanceProvider) -> dict[str, Any]:
        if not version_satisfies(provider.contract_version, "^1.0.0"):
            raise AssertionError(f"{provider.name} does not implement {SOCIAL_PUBLISHER_CONTRACT} v1.")
        if not 0 < provider.execution_timeout_seconds <= 300:
            raise AssertionError(f"{provider.name} does not declare a bounded execution timeout.")

        accounts = provider.invoke("accounts.list", {})["accounts"]
        if not accounts:
            raise AssertionError(f"{provider.name} returned no publishable account for conformance.")
        account_id = str(accounts[0]["id"])
        window = {"start_date": "2026-01-01", "end_date": "2026-12-31"}
        created = provider.invoke("posts.create", {"account_id": account_id, "platform": "linkedin", "content": "Conformance publish"})["post"]
        looked_up = provider.invoke("posts.get", {"post_id": created["id"], **window})["post"]
        scheduled = provider.invoke("posts.schedule", {"account_id": account_id, "platform": "linkedin", "content": "Conformance schedule", "scheduled_for": "2026-08-22T10:00:00Z"})["post"]
        cancelled = provider.invoke("posts.cancel", {"post_id": scheduled["id"]})["post"]
        health = provider.invoke("health", {})
        self._assert_normalized_error(provider, "posts.create", {}, IntegrationErrorCategory.INVALID_REQUEST)
        checks = ["accounts", "create", "get_with_lookup_window", "schedule", "cancel", "health", "invalid_request"]
        if provider.supports_media_upload:
            uploaded = provider.invoke("media.upload", {
                "name": "conformance.mp4", "content_type": "video/mp4",
                "data_base64": base64.b64encode(b"\x00\x00\x00 ftypconformance").decode(),
            })
            media_record = uploaded.get("media") if isinstance(uploaded, dict) else None
            if not isinstance(media_record, dict) or not str(media_record.get("id") or ""):
                raise AssertionError(f"{provider.name} media.upload did not return a media id.")
            checks.append("media_upload")
        else:
            self._assert_normalized_error(provider, "media.upload", {}, IntegrationErrorCategory.UNSUPPORTED_FEATURE)
        self._assert_redacted((accounts, created, looked_up, scheduled, cancelled, health), provider.secrets)
        self._assert_lifecycle(provider)
        return {
            "provider": provider.name,
            "contract": SOCIAL_PUBLISHER_CONTRACT,
            "version": provider.contract_version,
            "direct_lookup": provider.direct_lookup,
            "checks": checks + ["redaction", "lifecycle"],
        }

    @staticmethod
    def _assert_normalized_error(provider: SocialPublisherConformanceProvider, operation: str, payload: dict[str, Any], category: IntegrationErrorCategory) -> None:
        try:
            provider.invoke(operation, payload)
        except IntegrationExternalError as exc:
            if exc.category is not category:
                raise AssertionError(f"{provider.name} returned {exc.category} for {operation}, expected {category}.") from exc
            return
        raise AssertionError(f"{provider.name} accepted invalid or unsupported {operation}.")

    @staticmethod
    def _assert_redacted(values: Iterable[Any], secrets: Iterable[str]) -> None:
        rendered = repr(tuple(values))
        for secret in secrets:
            if secret and secret in rendered:
                raise AssertionError("Provider returned a configured credential value.")

    @staticmethod
    def _assert_lifecycle(provider: SocialPublisherConformanceProvider) -> None:
        lifecycle = provider.lifecycle
        if lifecycle is None:
            return
        if lifecycle.registration_count() != 1:
            raise AssertionError(f"{provider.name} must begin with exactly one active contribution.")
        lifecycle.disable()
        try:
            provider.invoke("accounts.list", {})
        except (PlatformNotFoundError, PlatformOperationError):
            pass
        else:
            raise AssertionError(f"{provider.name} remained available after disable.")
        lifecycle.enable()
        if lifecycle.registration_count() != 1:
            raise AssertionError(f"{provider.name} re-enabled with duplicate contributions.")
        lifecycle.restart()
        if lifecycle.registration_count() != 1:
            raise AssertionError(f"{provider.name} restart recovery duplicated contributions.")
