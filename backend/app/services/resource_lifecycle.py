"""Provider-neutral, idempotent resource lifecycle orchestration."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.services.resource_bindings import ResourceBindingService
from app.services.resource_outbox import ResourceOutboxService
from app.services.resource_setup_requirements import ResourceSetupRequirementService
from app.services.resource_reconciliation import ResourceReconciliationService
from app.services.resource_repair import ResourceRepairPolicy
from app.services.platform_services import PlatformConflictError, PlatformOperationError, PlatformValidationError


@dataclass(frozen=True, slots=True)
class ExternalResource:
    external_id: str
    url: str = ""
    ownership_mode: str = "managed"
    observed_fingerprint: str = ""
    setup_required: tuple[dict[str, Any], ...] = ()


class ResourceLifecycleProvider(Protocol):
    integration_id: str
    role: str

    def discover(self, *, payload: dict[str, Any], idempotency_key: str) -> ExternalResource | None: ...

    def provision(self, *, payload: dict[str, Any], idempotency_key: str) -> ExternalResource: ...

    def verify(self, *, external: ExternalResource, payload: dict[str, Any]) -> bool: ...

    def retire(self, *, external: ExternalResource, idempotency_key: str) -> None: ...


class ResourceLifecycleCoordinator:
    """Consumes one claimed resource outbox item at a time.

    The caller owns scheduling. This class is intentionally a worker contract,
    not a new daemon or polling loop.
    """

    def __init__(
        self,
        outbox: ResourceOutboxService,
        bindings: ResourceBindingService,
        setup: ResourceSetupRequirementService | None = None,
        *,
        provider_resolver: Any,
    ) -> None:
        self.outbox = outbox
        self.bindings = bindings
        self.setup = setup or ResourceSetupRequirementService(outbox.store, tenant_id=outbox.tenant_id)
        self.reconciliation = ResourceReconciliationService(outbox.store, tenant_id=outbox.tenant_id)
        self.repair_policy = ResourceRepairPolicy()
        self.provider_resolver = provider_resolver

    def reconcile_attention(self, *, limit: int = 200) -> dict[str, int]:
        return self.reconciliation.reconcile(bindings=self.bindings, outbox=self.outbox, limit=limit)

    def reconcile_bindings(
        self,
        *,
        max_items: int = 5,
        integration_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Rediscover and verify existing bindings without provisioning."""
        if max_items < 1:
            return []
        results: list[dict[str, Any]] = []
        candidates = [
            item for item in self.bindings.list()
            if item.get("state") in {"READY", "DEGRADED"}
            and item.get("external_resource_id")
            and (integration_id is None or item.get("integration_id") == integration_id)
        ]
        for binding in candidates[:max_items]:
            item = {
                "id": f"reconcile:{binding['id']}", "aggregate_type": binding["resource_type"],
                "aggregate_id": binding["resource_id"], "aggregate_revision": binding["revision"],
                "event_type": f"resource.{binding['resource_type']}.reconcile", "project_id": binding.get("project_id"),
            }
            provider = self.provider_resolver(item)
            if provider is None:
                results.append({"binding_id": binding["id"], "action": "provider_unavailable"})
                continue
            try:
                external = provider.discover(payload={"resource_id": binding["resource_id"], "reconcile": True}, idempotency_key=f"reconcile:{binding['id']}:{binding['revision']}")
                if external is None:
                    changed = self.bindings.transition(binding["id"], state="DEGRADED", expected_revision=int(binding["revision"]), error_code="external_missing", error_message="Provider discovery did not find the bound resource.")
                    results.append({"binding_id": changed["id"], "action": "external_missing"})
                    continue
                if binding.get("desired_fingerprint") and external.observed_fingerprint and binding["desired_fingerprint"] != external.observed_fingerprint:
                    decision = self.repair_policy.decide(binding=binding, provider=provider, observed_fingerprint=external.observed_fingerprint)
                    if decision.allowed:
                        try:
                            repaired = provider.repair(
                                external=external,
                                payload={"resource_id": binding["resource_id"], "desired_fingerprint": binding["desired_fingerprint"], "reconcile": True},
                                idempotency_key=f"repair:{binding['id']}:{binding['revision']}",
                            )
                            if repaired is not None and repaired.observed_fingerprint == binding["desired_fingerprint"] and provider.verify(external=repaired, payload={"resource_id": binding["resource_id"], "repair": True}):
                                repair_metadata = dict(binding.get("metadata") or {})
                                repair_metadata["automatic_repair_count"] = int(repair_metadata.get("automatic_repair_count", 0)) + 1
                                repair_metadata.update({"repair": "automatic", "repair_code": decision.code})
                                changed = self.bindings.transition(binding["id"], state="READY", expected_revision=int(binding["revision"]), external_resource_id=repaired.external_id, external_resource_url=repaired.url, observed_fingerprint=repaired.observed_fingerprint, metadata=repair_metadata)
                                results.append({"binding_id": changed["id"], "action": "auto_repaired"})
                                continue
                        except PlatformOperationError:
                            pass
                    changed = self.bindings.transition(binding["id"], state="DEGRADED", expected_revision=int(binding["revision"]), external_resource_id=external.external_id, external_resource_url=external.url, observed_fingerprint=external.observed_fingerprint, error_code="drift_detected", error_message="Desired and observed provider state differ.")
                    results.append({"binding_id": changed["id"], "action": "drift_detected", "repair": decision.code})
                    continue
                if not provider.verify(external=external, payload={"resource_id": binding["resource_id"], "reconcile": True}):
                    changed = self.bindings.transition(binding["id"], state="DEGRADED", expected_revision=int(binding["revision"]), external_resource_id=external.external_id, external_resource_url=external.url, observed_fingerprint=external.observed_fingerprint, error_code="verification_failed", error_message="Provider verification did not pass during reconciliation.")
                    results.append({"binding_id": changed["id"], "action": "verification_failed"})
                    continue
                changed = self.bindings.transition(binding["id"], state="READY", expected_revision=int(binding["revision"]), external_resource_id=external.external_id, external_resource_url=external.url, observed_fingerprint=external.observed_fingerprint)
                results.append({"binding_id": changed["id"], "action": "ready"})
            except PlatformConflictError:
                results.append({"binding_id": binding["id"], "action": "stale_revision"})
            except PlatformOperationError:
                results.append({"binding_id": binding["id"], "action": "provider_error"})
        return results

    def repair_binding(
        self,
        *,
        binding_id: str,
        operator_id: str,
        reason: str,
    ) -> dict[str, Any]:
        """Run one explicitly authorized repair for an ambiguous binding.

        Automatic repair stays limited to managed drift. This path requires an
        operator reason and permits a provider-supported repair for managed or
        linked resources, while observed resources remain non-destructive.
        Revision fencing ensures a concurrent reconciliation cannot overwrite a
        newer binding state.
        """
        if not operator_id.strip() or len(operator_id) > 160 or not reason.strip() or len(reason) > 2_000:
            raise PlatformValidationError("Binding repair requires a bounded operator id and reason.")
        binding = self.bindings.get(binding_id)
        if binding.get("state") != "DEGRADED":
            raise PlatformConflictError("Only degraded bindings can be repaired.")
        if binding.get("ownership_mode") not in {"managed", "linked"}:
            raise PlatformConflictError("Observed bindings require a separate ownership confirmation.")
        provider = self.provider_resolver({
            "id": f"operator-repair:{binding_id}",
            "aggregate_type": binding["resource_type"],
            "aggregate_id": binding["resource_id"],
            "aggregate_revision": binding["revision"],
            "project_id": binding.get("project_id"),
            "integration_id": binding.get("integration_id"),
            "connection_id": binding.get("connection_id"),
            "event_type": f"resource.{binding['resource_type']}.repair",
        })
        if provider is None or not callable(getattr(provider, "repair", None)):
            raise PlatformOperationError("A provider-supported binding repair is not available.")
        external = provider.discover(
            payload={"resource_id": binding["resource_id"], "operator_repair": True},
            idempotency_key=f"operator-repair-discover:{binding_id}:{binding['revision']}",
        )
        if external is None:
            raise PlatformOperationError("The provider resource could not be rediscovered for repair.")
        repaired = provider.repair(
            external=external,
            payload={
                "resource_id": binding["resource_id"],
                "desired_fingerprint": binding.get("desired_fingerprint") or "",
                "operator_repair": True,
                "reason": reason[:2_000],
            },
            idempotency_key=f"operator-repair:{binding_id}:{binding['revision']}",
        )
        desired = str(binding.get("desired_fingerprint") or "")
        if repaired is None or (desired and repaired.observed_fingerprint != desired) or not provider.verify(
            external=repaired,
            payload={"resource_id": binding["resource_id"], "operator_repair": True},
        ):
            raise PlatformOperationError("Provider repair did not produce a verified resource state.")
        metadata = dict(binding.get("metadata") or {})
        metadata.update({"repair": "operator", "repair_operator": operator_id[:160]})
        changed = self.bindings.transition(
            binding_id,
            state="READY",
            expected_revision=int(binding["revision"]),
            external_resource_id=repaired.external_id,
            external_resource_url=repaired.url,
            observed_fingerprint=repaired.observed_fingerprint,
            metadata=metadata,
        )
        return {"binding_id": changed["id"], "action": "operator_repaired", "reason": reason[:2_000]}

    def verify_setup(self, *, binding_id: str, requirement_id: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        """Verify one setup requirement through the provider, then promote the binding."""
        binding = self.bindings.get(binding_id)
        requirement = self.setup.get(requirement_id)
        if requirement.get("binding_id") != binding_id:
            raise PlatformConflictError("Setup requirement is not attached to this binding.")
        provider = self.provider_resolver({
            "id": f"verify:{binding_id}", "aggregate_type": binding["resource_type"],
            "aggregate_id": binding["resource_id"], "project_id": binding.get("project_id"),
            "event_type": f"resource.{binding['resource_type']}.verify",
        })
        if provider is None or not binding.get("external_resource_id"):
            failed = self.setup.transition(requirement_id, state="FAILED", expected_revision=int(requirement["revision"]), error_code="provider_unavailable", error_message="Provider is unavailable for setup verification.")
            current = self.bindings.get(binding_id)
            self.bindings.transition(binding_id, state="DEGRADED", expected_revision=int(current["revision"]), error_code="setup_verification_unavailable", error_message="Setup verification could not reach the provider.")
            return {"binding_id": binding_id, "requirement_id": failed["id"], "action": "verification_unavailable"}
        external = provider.discover(payload={"resource_id": binding["resource_id"], "verify_setup": True}, idempotency_key=f"verify_setup:{binding_id}:{requirement['revision']}")
        passed = external is not None and provider.verify(external=external, payload={"resource_id": binding["resource_id"], "verify_setup": True})
        if not passed:
            failed = self.setup.transition(requirement_id, state="FAILED", expected_revision=int(requirement["revision"]), error_code="verification_failed", error_message="Provider verification did not pass after setup.", verification=evidence or {})
            current = self.bindings.get(binding_id)
            self.bindings.transition(binding_id, state="DEGRADED", expected_revision=int(current["revision"]), error_code="setup_verification_failed", error_message="Provider verification did not pass after setup.")
            return {"binding_id": binding_id, "requirement_id": failed["id"], "action": "verification_failed"}
        verified = self.setup.transition(requirement_id, state="VERIFIED", expected_revision=int(requirement["revision"]), verification=evidence or {"provider_verified": True})
        remaining = [item for item in self.setup.list(binding_id=binding_id) if item["state"] != "VERIFIED"]
        if remaining:
            return {"binding_id": binding_id, "requirement_id": verified["id"], "action": "requirement_verified", "remaining": len(remaining)}
        current = self.bindings.get(binding_id)
        ready = self.bindings.transition(binding_id, state="READY", expected_revision=int(current["revision"]), external_resource_id=external.external_id, external_resource_url=external.url, observed_fingerprint=external.observed_fingerprint, metadata={"setup_verified": True, "evidence": evidence or {}})
        return {"binding_id": ready["id"], "requirement_id": verified["id"], "action": "ready"}

    def process_one(self, *, worker_id: str, lease_seconds: int = 60) -> dict[str, Any] | None:
        item = self.outbox.claim_next(worker_id=worker_id, lease_seconds=lease_seconds)
        if item is None:
            return None
        try:
            result = self._process(item)
            self.outbox.complete(outbox_id=item["id"], worker_id=worker_id, lease_token=item["lease_token"])
            return {"outbox_id": item["id"], "status": "processed", **result}
        except PlatformOperationError as exc:
            self.outbox.retry(
                outbox_id=item["id"], worker_id=worker_id, lease_token=item["lease_token"],
                error_code=getattr(exc, "code", "lifecycle_error"), error_message=str(exc),
            )
            return {"outbox_id": item["id"], "status": "retrying", "error": type(exc).__name__}
        except Exception as exc:
            self.outbox.dead_letter(
                outbox_id=item["id"], worker_id=worker_id, lease_token=item["lease_token"],
                error_code="lifecycle_unexpected", error_message=type(exc).__name__,
            )
            return {"outbox_id": item["id"], "status": "dead_letter", "error": type(exc).__name__}

    def _process(self, item: dict[str, Any]) -> dict[str, Any]:
        payload = dict(item.get("payload") or {})
        provider = self.provider_resolver(item)
        if provider is None:
            raise PlatformOperationError("No ready provider is available for this resource lifecycle operation.")
        resource_type = str(item["aggregate_type"])
        resource_id = str(item["aggregate_id"])
        binding = self.bindings.find(
            resource_type=resource_type,
            resource_id=resource_id,
            role=provider.role,
            integration_id=provider.integration_id,
        )
        if item["event_type"].endswith(".deleted"):
            if binding is None or not binding.get("external_resource_id"):
                return {"binding_id": binding["id"] if binding else None, "action": "detached"}
            provider.retire(
                external=ExternalResource(
                    str(binding["external_resource_id"]), str(binding.get("external_resource_url") or ""),
                    str(binding.get("ownership_mode") or "managed"), str(binding.get("observed_fingerprint") or ""),
                ),
                idempotency_key=f"{item['id']}:retire",
            )
            if binding["state"] not in {"RETIRING", "DETACHED"}:
                binding = self.bindings.transition(binding["id"], state="RETIRING", expected_revision=int(binding["revision"]))
            return {"binding_id": binding["id"], "action": "retired"}

        if binding is None:
            binding = self.bindings.create(
                project_id=str(item.get("project_id") or "") or None,
                resource_type=resource_type,
                resource_id=resource_id,
                role=provider.role,
                integration_id=provider.integration_id,
                state="DISCOVERING",
            )
        external = provider.discover(payload=payload, idempotency_key=f"{item['id']}:discover")
        if external is None:
            binding = self.bindings.transition(binding["id"], state="PROVISIONING", expected_revision=int(binding["revision"]))
            external = provider.provision(payload=payload, idempotency_key=f"{item['id']}:provision")
        binding = self.bindings.transition(
            binding["id"], state="VERIFYING", expected_revision=int(binding["revision"]),
            external_resource_id=external.external_id, external_resource_url=external.url,
            observed_fingerprint=external.observed_fingerprint,
        )
        for requirement in external.setup_required:
            self.setup.ensure(
                binding_id=binding["id"], requirement_type=str(requirement.get("type") or "manual_instruction"),
                instructions=dict(requirement.get("instructions") or {}), verification=dict(requirement.get("verification") or {}),
                state="MANUAL_REQUIRED",
            )
        if external.setup_required:
            waiting = self.bindings.transition(
                binding["id"], state="SETUP_REQUIRED", expected_revision=int(binding["revision"]),
            )
            return {"binding_id": waiting["id"], "action": "setup_required"}
        if not provider.verify(external=external, payload=payload):
            return {"binding_id": self.bindings.transition(binding["id"], state="DEGRADED", expected_revision=int(binding["revision"]), error_code="verification_failed", error_message="Provider verification did not pass.")["id"], "action": "degraded"}
        ready = self.bindings.transition(
            binding["id"], state="READY", expected_revision=int(binding["revision"]),
            external_resource_id=external.external_id, external_resource_url=external.url,
        )
        return {"binding_id": ready["id"], "action": "ready", "external_resource_id": external.external_id}


__all__ = ["ExternalResource", "ResourceLifecycleCoordinator", "ResourceLifecycleProvider"]
