"""Explicit dry-run and reversible activation-definition upgrades."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from app.services.integration_contributions import ContributionRegistry
from app.services.integration_definition_identity import IntegrationDefinitionIdentity
from app.services.extension_manifest import version_satisfies
from app.services.integration_capability_evidence import IntegrationCapabilityEvidenceService
from app.services.integration_lifecycle import IntegrationActivationContext, IntegrationLifecycleRuntime, IntegrationRuntimeDefinition
from app.services.platform_services import PlatformOperationError, PlatformValidationError


ConformanceCheck = Callable[[IntegrationRuntimeDefinition], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class IntegrationDefinitionUpgradePreview:
    integration_id: str
    current: dict[str, Any]
    candidate: dict[str, Any]
    diff: dict[str, dict[str, Any]]
    conformance: dict[str, Any]
    dry_run: dict[str, Any]


class IntegrationDefinitionUpgradeService:
    """A small service primitive; upgrades never happen during normal recovery."""

    def __init__(self, runtime: IntegrationLifecycleRuntime) -> None:
        self.runtime = runtime

    def preview(
        self,
        integration_id: str,
        candidate: IntegrationRuntimeDefinition,
        *,
        conformance: ConformanceCheck | None = None,
    ) -> IntegrationDefinitionUpgradePreview:
        if candidate.integration_id != integration_id:
            raise PlatformValidationError("Upgrade candidate belongs to a different integration.")
        current_identity = self.runtime.identities.active(integration_id)
        if current_identity is None:
            raise PlatformOperationError("Only an activated integration definition can be upgraded.")
        candidate_identity = self._register_candidate(candidate)
        current = self.runtime.definition_for_identity(current_identity)
        if current is None:
            raise PlatformOperationError("The current activated definition is unavailable for rollback.")
        self._ensure_contract_compatibility(current_identity, candidate_identity)
        conformance_result = conformance(candidate) if conformance is not None else {"status": "not_requested"}
        if not isinstance(conformance_result, dict):
            raise PlatformValidationError("Upgrade conformance check must return an object.")
        dry_run_result = self._dry_run(candidate)
        return IntegrationDefinitionUpgradePreview(
            integration_id=integration_id,
            current=asdict(current_identity),
            candidate=asdict(candidate_identity),
            diff=self._diff(current_identity, candidate_identity),
            conformance=conformance_result,
            dry_run=dry_run_result,
        )

    def apply(
        self,
        integration_id: str,
        candidate: IntegrationRuntimeDefinition,
        *,
        conformance: ConformanceCheck | None = None,
    ) -> dict[str, Any]:
        preview = self.preview(integration_id, candidate, conformance=conformance)
        current_identity = self.runtime.identities.active(integration_id)
        assert current_identity is not None
        was_active = self.runtime.is_active(integration_id)
        if was_active:
            self.runtime.deactivate(integration_id)
        self.runtime.identities.replace_active(IntegrationDefinitionIdentity(**preview.candidate))
        try:
            result = self.runtime.activate(integration_id) if self.runtime.integrations.get(integration_id)["enabled"] else self.runtime.integrations.get(integration_id)
        except Exception as exc:
            self.runtime.identities.replace_active(current_identity)
            if was_active:
                self.runtime.activate(integration_id)
            raise PlatformOperationError("Definition upgrade failed; previous activation was restored.") from exc
        return {"preview": asdict(preview), "integration": result}

    def _register_candidate(self, candidate: IntegrationRuntimeDefinition) -> IntegrationDefinitionIdentity:
        identity = self.runtime.identities.identity_for(
            candidate.integration_id,
            definition_version=candidate.definition_version,
            manifest_fingerprint=candidate.manifest_fingerprint,
            contract_versions=candidate.contract_versions,
        )
        if self.runtime.definition_for_identity(identity) is None:
            self.runtime.register_definition(candidate)
        return identity

    def _dry_run(self, candidate: IntegrationRuntimeDefinition) -> dict[str, Any]:
        if candidate.dry_run is None:
            return {"status": "not_provided"}
        integration = self.runtime.integrations.get(candidate.integration_id)
        isolated = ContributionRegistry(self.runtime.integrations)
        context = IntegrationActivationContext(
            integration_id=candidate.integration_id,
            configuration=dict(integration["configuration"]),
            contributions=isolated,
        )
        try:
            result = self.runtime.execution.run(
                "upgrade.dry_run", candidate.dry_run, context,
                timeout_seconds=candidate.execution_timeout_seconds,
            )
            if not isinstance(result, dict):
                raise PlatformValidationError("Definition dry-run must return an object.")
            return result
        finally:
            isolated.unregister_integration(candidate.integration_id)

    @staticmethod
    def _diff(current: IntegrationDefinitionIdentity, candidate: IntegrationDefinitionIdentity) -> dict[str, dict[str, Any]]:
        return {
            field: {"from": getattr(current, field), "to": getattr(candidate, field)}
            for field in ("definition_version", "manifest_fingerprint", "contract_versions")
            if getattr(current, field) != getattr(candidate, field)
        }

    @staticmethod
    def _ensure_contract_compatibility(current: IntegrationDefinitionIdentity, candidate: IntegrationDefinitionIdentity) -> None:
        for contract, version in current.contract_versions.items():
            replacement = candidate.contract_versions.get(contract)
            if replacement is None or not version_satisfies(replacement, f"^{version}"):
                raise PlatformValidationError(f"Upgrade candidate is incompatible with activated {contract} {version}.")

    def deployed_reconcile_preview(self, integration_id: str, candidate: IntegrationRuntimeDefinition) -> dict[str, Any]:
        """Report the lock move an immutable release requires. Writes nothing."""
        current_identity, candidate_identity = self._reconcile_identities(integration_id, candidate)
        self._ensure_contract_compatibility(current_identity, candidate_identity)
        capability_change = self._deployed_capability_change(integration_id, current_identity)
        return {
            "mode": "deployed_reconcile",
            "integration_id": integration_id,
            "current": asdict(current_identity),
            "candidate": asdict(candidate_identity),
            "diff": self._diff(current_identity, candidate_identity),
            "capability_change": capability_change,
            # Stated rather than implied: the previous definition's code is not
            # in this process, so there is nothing here to roll back to.
            "rollback": "redeploy_previous_release",
        }

    def reconcile_to_deployed(
        self, integration_id: str, candidate: IntegrationRuntimeDefinition,
    ) -> dict[str, Any]:
        """Move the activation lock onto the definition this release deploys.

        ``apply`` cannot serve this case. It requires the currently activated
        definition to still be registered so it can restore the object on
        failure, and after an immutable release that code is gone -- which is
        precisely when the lock needs moving. Rolling back here means
        deploying the previous release again, not restoring an object.

        Everything else that makes an upgrade safe is kept: the contract
        compatibility gate, a candidate compiled from the installed manifest,
        and restoring the previous lock if the new definition fails to
        activate.
        """
        current_identity, candidate_identity = self._reconcile_identities(integration_id, candidate)
        self._ensure_contract_compatibility(current_identity, candidate_identity)
        capability_change = self._deployed_capability_change(integration_id, current_identity)
        if self.runtime.is_active(integration_id):
            self.runtime.deactivate(integration_id)
        self.runtime.identities.replace_active(candidate_identity)
        try:
            enabled = bool(self.runtime.integrations.get(integration_id)["enabled"])
            result = (
                self.runtime.activate(integration_id) if enabled
                else self.runtime.integrations.get(integration_id)
            )
        except Exception as exc:
            self.runtime.identities.replace_active(current_identity)
            raise PlatformOperationError(
                "Definition reconcile failed; the previous activation lock was restored."
            ) from exc
        return {
            "mode": "deployed_reconcile",
            "integration_id": integration_id,
            "current": asdict(current_identity),
            "candidate": asdict(candidate_identity),
            "diff": self._diff(current_identity, candidate_identity),
            "capability_change": capability_change,
            "rollback": "redeploy_previous_release",
            "integration": result,
        }

    def _deployed_capability_change(
        self, integration_id: str, current: IntegrationDefinitionIdentity,
    ) -> dict[str, Any]:
        """Compare the candidate declaration with the last activated declaration.

        Capability evidence is keyed by capability rather than by definition,
        so old entries remain available after an immutable deploy. Only entries
        recorded for the activation lock's version can describe its declaration.
        If that evidence is incomplete, the result is unknown and callers must
        keep the review path.
        """
        manifest = self.runtime.integrations.get(integration_id).get("manifest") or {}
        candidate: dict[str, list[str]] = {}
        for provided in manifest.get("provides", []):
            if not isinstance(provided, dict):
                continue
            contract = str(provided.get("contract") or "")
            if contract:
                candidate[contract] = sorted({str(value) for value in provided.get("capabilities", [])})

        evidence = IntegrationCapabilityEvidenceService(self.runtime.integrations).list(
            integration_id=integration_id,
        )
        current_capabilities: dict[str, list[str]] = {}
        for row in evidence:
            if str(row.get("definition_version") or "") != current.definition_version:
                continue
            contract = str(row.get("contract") or "")
            capability = str(row.get("capability") or "")
            if contract and capability:
                current_capabilities.setdefault(contract, []).append(capability)
        current_capabilities = {
            contract: sorted(set(values))
            for contract, values in current_capabilities.items()
        }
        expected_current_contracts = set(current.contract_versions)
        known = expected_current_contracts.issubset(current_capabilities)
        changed = (
            not known
            or set(current_capabilities) != set(candidate)
            or current_capabilities != candidate
        )
        return {
            "known": known,
            "changed": changed,
            "current": current_capabilities if known else {},
            "candidate": candidate,
            "added": {
                contract: sorted(set(candidate.get(contract, [])) - set(current_capabilities.get(contract, [])))
                for contract in sorted(set(candidate) | set(current_capabilities))
                if set(candidate.get(contract, [])) - set(current_capabilities.get(contract, []))
            },
            "removed": {
                contract: sorted(set(current_capabilities.get(contract, [])) - set(candidate.get(contract, [])))
                for contract in sorted(set(candidate) | set(current_capabilities))
                if set(current_capabilities.get(contract, [])) - set(candidate.get(contract, []))
            },
        }

    def _reconcile_identities(
        self, integration_id: str, candidate: IntegrationRuntimeDefinition,
    ) -> tuple[IntegrationDefinitionIdentity, IntegrationDefinitionIdentity]:
        if candidate.integration_id != integration_id:
            raise PlatformValidationError("Upgrade candidate belongs to a different integration.")
        current_identity = self.runtime.identities.active(integration_id)
        if current_identity is None:
            raise PlatformOperationError("Only an activated integration definition can be upgraded.")
        candidate_identity = self._register_candidate(candidate)
        if candidate_identity == current_identity:
            raise PlatformOperationError("The deployed definition is already the activated one.")
        if self.runtime.definition_for_identity(current_identity) is not None:
            # Both definitions are present, so the reversible path applies and
            # the caller should not be giving up its rollback.
            raise PlatformOperationError(
                "The previously activated definition is still registered; use the reversible upgrade."
            )
        return current_identity, candidate_identity
