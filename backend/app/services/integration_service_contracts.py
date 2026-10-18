"""Versioned provider-neutral service contracts over owned contributions."""
from __future__ import annotations

from typing import Any

from app.services.extension_manifest import version_satisfies
from app.services.integration_capability_evidence import IntegrationCapabilityEvidenceService
from app.services.integration_contributions import Contribution, ContributionRegistry
from app.services.integration_errors import IntegrationErrorCategory, integration_error
from app.services.integration_provider_selection import IntegrationProviderSelectionService
from app.services.integration_async import decorate_async_result
from app.services.platform_services import PlatformNotFoundError, PlatformOperationError, PlatformValidationError


class IntegrationServiceContractRegistry:
    """Resolves a single compatible service contribution without direct imports."""

    def __init__(
        self,
        contributions: ContributionRegistry,
        *,
        evidence: IntegrationCapabilityEvidenceService | None = None,
        selections: IntegrationProviderSelectionService | None = None,
        sdk_factory: Any | None = None,
    ) -> None:
        self.contributions = contributions
        self.evidence = evidence
        self.selections = selections
        # Host-owned. Only the factory may build an SDK, and only from the
        # actor and context of the request being served.
        self.sdk_factory = sdk_factory

    def _request_sdk(
        self, provider: Contribution, *, project_id: str | None, actor: Any, invocation_context: Any,
    ) -> Any:
        """Build the SDK for this one invocation, or refuse to invoke at all.

        Fails closed on purpose. An SDK-scoped provider exists precisely because
        it needs tenant, project, and actor authority; serving it without one
        would either invent authority or reuse whatever the process last had.
        Refusing is the only safe answer.
        """
        if self.sdk_factory is None:
            raise PlatformOperationError(
                "This provider requires a request-scoped SDK and no SDK factory is available."
            )
        if actor is None:
            raise PlatformValidationError(
                "This provider requires an authenticated actor for the invocation."
            )
        return self.sdk_factory.create(
            caller=actor,
            integration_id=provider.integration_id,
            # Taken from the invocation context, which is the host-built
            # carrier of scope. A provider that needs a connection needs *this*
            # request's connection, and deriving it anywhere else would let one
            # request be served through another's credentials.
            connection_id=getattr(invocation_context, "connection_id", None),
            project_id=project_id,
            invocation_context=invocation_context,
        )

    def resolve(
        self,
        contract: str,
        version_range: str | None = None,
        *,
        project_id: str | None = None,
        provider_integration_id: str | None = None,
    ) -> Contribution:
        if not contract.startswith("lynewright."):
            raise PlatformValidationError("Integration service contract must use the lynewright namespace.")
        providers = [
            item for item in self.contributions.list(kind="service")
            if item.metadata.get("contract") == contract
            and isinstance(item.metadata.get("version"), str)
            # Version selection is opt-in.  A contract caller that has a
            # compatibility requirement supplies a range; a generic caller
            # must not silently assume all integrations start at 1.0.0 and
            # thereby hide an active, declared 0.x provider.
            and (version_range is None or version_satisfies(str(item.metadata["version"]), version_range))
        ]
        # A host path operating an integration's declared resource descriptor
        # must invoke that descriptor's owner. Provider selection is for a
        # consumer choosing between providers, not for discovery of one
        # particular provider's external resources.
        if provider_integration_id is not None:
            providers = [item for item in providers if item.integration_id == provider_integration_id]
        elif self.selections is not None:
            # A selection is authoritative when one exists.  When there is no
            # selection, a single active contribution is safe to resolve by
            # capability alone; requiring an operator to create a redundant
            # provider-selection row made every newly installed integration
            # appear unavailable even when it was the only compatible provider.
            # Ambiguity still fails closed below, and a stale explicit
            # selection never silently falls back to another provider.
            try:
                selection = self.selections.get(contract=contract, project_id=project_id)
            except PlatformNotFoundError:
                selection = None
            if selection is not None:
                selected_id = str(selection["integration_id"])
                providers = [item for item in providers if item.integration_id == selected_id]
                if not providers:
                    raise integration_error(
                        IntegrationErrorCategory.PROVIDER_UNAVAILABLE,
                        diagnostic={"provider": selected_id, "contract": contract, "project_id": project_id or ""},
                    )
        # A project-scoped provider is an authoritative binding, not a hint.
        # Prefer an exact project match whenever the provider declares one,
        # while keeping global providers available for projects that have no
        # project-bound provider.  This is deliberately contract-neutral: any
        # future integration can declare ``project_id`` or ``project_ids`` in
        # its contribution metadata and receives the same safe resolution.
        # Providers bound to another project are never eligible for this one.
        if provider_integration_id is None and project_id:
            project = str(project_id).strip()
            scoped: list[Contribution] = []
            global_providers: list[Contribution] = []
            for item in providers:
                metadata = item.metadata if isinstance(item.metadata, dict) else {}
                declared = metadata.get("project_id")
                declared_many = metadata.get("project_ids")
                project_ids: set[str] = set()
                if declared not in (None, ""):
                    project_ids.add(str(declared).strip())
                if isinstance(declared_many, (list, tuple, set, frozenset)):
                    project_ids.update(
                        str(value).strip() for value in declared_many if value not in (None, "")
                    )
                project_ids.discard("")
                if not project_ids:
                    global_providers.append(item)
                elif project in project_ids:
                    scoped.append(item)
            if scoped:
                providers = scoped
            elif any(
                (item.metadata if isinstance(item.metadata, dict) else {}).get("project_id") not in (None, "")
                or bool((item.metadata if isinstance(item.metadata, dict) else {}).get("project_ids"))
                for item in providers
            ):
                # There are project-bound candidates, but none serves this
                # project.  Do not silently route to another project's data.
                providers = global_providers
        if not providers:
            raise PlatformNotFoundError("No compatible integration service provider is active.")
        if len(providers) > 1:
            # Preserve the historical not-found contract for callers that use
            # this as a selection prompt, while still failing closed: an
            # ambiguous provider set is never treated as an implicit choice.
            raise PlatformNotFoundError(
                "Multiple compatible integration service providers are active; select one explicitly."
            )
        return providers[0]

    def invoke(
        self,
        contract: str,
        operation: str,
        payload: dict[str, Any] | None = None,
        *,
        version_range: str | None = None,
        project_id: str | None = None,
        require_verified: bool = False,
        actor: Any | None = None,
        invocation_context: Any | None = None,
        provider_integration_id: str | None = None,
    ) -> dict[str, Any]:
        if not operation or len(operation) > 120:
            raise PlatformValidationError("Integration service operation is invalid.")
        provider = self.resolve(
            contract,
            version_range,
            project_id=project_id,
            provider_integration_id=provider_integration_id,
        )
        if self.evidence is not None:
            self.evidence.require_usable(
                integration_id=provider.integration_id,
                contract=contract,
                capability=operation,
                require_verified=require_verified,
            )
        if str(provider.metadata.get("invocation") or "direct") == "sdk_scoped":
            value = self.contributions.invoke(
                provider.id,
                self._request_sdk(provider, project_id=project_id, actor=actor, invocation_context=invocation_context),
                operation,
                dict(payload or {}),
                invocation_context=invocation_context,
            )
        else:
            # Direct and SDK-scoped contributions receive the same request
            # context.  Losing it on the direct path breaks worker routing,
            # idempotency, audit identity, and provider-declared async state.
            value = self.contributions.invoke(
                provider.id, operation, dict(payload or {}),
                invocation_context=invocation_context,
            )
        if not isinstance(value, dict):
            raise PlatformValidationError("Integration service providers must return an object.")
        # ``project_id`` is host scope and is deliberately removed from the
        # provider business payload by the Operator integration adapters.  It
        # must nevertheless survive into any durable async poll descriptor,
        # because status operations are scoped actions too.
        return decorate_async_result(
            value, provider.metadata, operation, payload, project_id=project_id,
        )
