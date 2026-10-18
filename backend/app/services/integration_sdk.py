"""Capability-gated SDK facades for generic integration adapters."""
from __future__ import annotations

import re
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, TypeVar

from app.services.application_logging import log_event
from app.services.capability_context import CapabilityInvocationContext
from app.services.integration_capabilities import IntegrationActor, IntegrationAuthorizationService, PlatformPolicy, ProjectScopePolicy
from app.services.integration_connections import ConnectionProfileService
from app.services.integration_registry import IntegrationRegistry
from app.services.integration_state import IntegrationStateService
from app.services.platform_capabilities import CapabilityActor
from app.services.platform_events import CoreEventBus
from app.services.platform_services import (
    AIService,
    CredentialService,
    EventService,
    HTTPService,
    JobService,
    MediaService,
    PlatformNotFoundError,
    PlatformOperationError,
    PlatformStore,
    PlatformValidationError,
    ProjectService,
    SettingsService,
)
from app.services.tenant_schema import DEFAULT_TENANT_ID

# A document input must stay small enough to read without straining a
# 512 MB container. Well under the transfer service's bounded-memory ceiling.
MANAGED_READ_MAX_BYTES = 20_000_000


_T = TypeVar("_T")
_EVENT_ACTION_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,99}$")
_SETTING_KEY_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,120}$")


@dataclass(frozen=True, slots=True)
class IntegrationAuditContext:
    caller_id: str
    integration_id: str
    connection_id: str | None
    project_id: str | None
    request_id: str = ""
    job_id: str | None = None
    trace_id: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            key: value
            for key, value in {
                "caller_id": self.caller_id,
                "integration_id": self.integration_id,
                "connection_id": self.connection_id or "",
                "project_id": self.project_id or "",
                "request_id": self.request_id,
                "trace_id": self.trace_id,
                "job_id": self.job_id or "",
            }.items()
            if value
        }


class _Facade:
    def __init__(self, sdk: "IntegrationSDK") -> None:
        self._sdk = sdk

    def _allow(self, capability: str, *, project_id: str | None = None) -> IntegrationActor:
        return self._sdk._allow(capability, project_id=project_id)


class IntegrationLogger(_Facade):
    def write(self, level: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        log_event(
            level=level,
            category="integration",
            source=self._sdk.integration_id,
            message=message,
            details={**self._sdk.audit_context.as_dict(), **(details or {})},
            project_id=self._sdk.project_id,
            tenant_id=self._sdk.tenant_id,
            correlation_id=self._sdk.audit_context.request_id,
        )


class IntegrationProjects(_Facade):
    def list(self) -> list[dict[str, Any]]:
        self._allow("projects:read")
        return self._sdk._projects.list()

    def get(self, project_id: str) -> dict[str, Any]:
        self._allow("projects:read", project_id=project_id)
        return self._sdk._projects.get(project_id)

    def create(self, value: dict[str, Any]) -> dict[str, Any]:
        self._allow("projects:create")
        return self._sdk._projects.create(value)

    def update(self, project_id: str, value: dict[str, Any]) -> dict[str, Any]:
        self._allow("projects:update", project_id=project_id)
        return self._sdk._projects.update(project_id, value)

    def delete(self, project_id: str, *, confirm_project_id: str) -> dict[str, Any]:
        self._allow("projects:delete", project_id=project_id)
        return self._sdk._projects.delete(project_id, confirm_project_id=confirm_project_id)


class IntegrationJobs(_Facade):
    def list(self, *, project_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        target_project = self._sdk._project(project_id)
        self._allow("jobs:read", project_id=target_project)
        return self._sdk._jobs.list(project_id=target_project, status=status)

    def get(self, job_id: str) -> dict[str, Any]:
        self._allow("jobs:read", project_id=self._sdk.project_id)
        return self._sdk._jobs.get(job_id)

    def create(self, value: dict[str, Any], *, project_id: str | None = None) -> dict[str, Any]:
        target_project = self._sdk._project(project_id)
        self._allow("jobs:create", project_id=target_project)
        return self._sdk._jobs.create(value, project_id=target_project, client_id=self._sdk.caller.client_id)

    def cancel(self, job_id: str) -> dict[str, Any]:
        self._allow("jobs:cancel", project_id=self._sdk.project_id)
        return self._sdk._jobs.cancel(job_id)

    def retry(self, job_id: str) -> dict[str, Any]:
        self._allow("jobs:retry", project_id=self._sdk.project_id)
        return self._sdk._jobs.retry(job_id)

    def start(self, job_id: str) -> dict[str, Any]:
        self._allow("jobs:create", project_id=self._sdk.project_id)
        return self._sdk._jobs.start(job_id)

    def report_progress(self, job_id: str, *, progress: int, summary: str = "") -> dict[str, Any]:
        self._allow("jobs:create", project_id=self._sdk.project_id)
        return self._sdk._jobs.report_progress(job_id, progress=progress, summary=summary)

    def complete(self, job_id: str, *, result: dict[str, Any] | None = None) -> dict[str, Any]:
        self._allow("jobs:create", project_id=self._sdk.project_id)
        return self._sdk._jobs.complete(job_id, result=result)

    def fail(self, job_id: str, *, code: str, message: str) -> dict[str, Any]:
        self._allow("jobs:create", project_id=self._sdk.project_id)
        return self._sdk._jobs.fail(job_id, code=code, message=message)


class IntegrationMedia(_Facade):
    def list(self, *, project_id: str | None = None) -> list[dict[str, Any]]:
        target_project = self._sdk._project(project_id)
        self._allow("media:read", project_id=target_project)
        return self._sdk._media.list(project_id=target_project)

    def get(self, media_id: str) -> dict[str, Any]:
        self._allow("media:read", project_id=self._sdk.project_id)
        return self._sdk._media.get(media_id)

    def create(self, value: dict[str, Any], *, project_id: str | None = None) -> dict[str, Any]:
        target_project = self._sdk._project(project_id)
        self._allow("media:create", project_id=target_project)
        return self._sdk._media.create(value, project_id=target_project)

    def create_managed(self, *, name: str, content_type: str, metadata: dict[str, Any], project_id: str | None = None) -> dict[str, Any]:
        """Register bytes already copied into Lynewright's confined media root."""
        target_project = self._sdk._project(project_id)
        self._allow("media:create", project_id=target_project)
        return self._sdk._media.create_managed(
            name=name, content_type=content_type, metadata=metadata, project_id=target_project,
        )

    def upload_artifact(self, *, name: str, content_type: str, body: bytes) -> dict[str, Any]:
        """Create managed output bytes through the host, or the runner upload API.

        Providers never write to Lynewright's media directory. The in-process
        implementation materializes the bytes in the host; RunnerSDK exposes
        the same method and sends them to the scoped invocation artifact route.
        """
        from app.services.integration_artifacts import MAX_ARTIFACT_BYTES, materialize_artifact_bytes

        target_project = self._sdk.project_id
        self._allow("media:create", project_id=target_project)
        if not isinstance(body, bytes) or not body or len(body) > MAX_ARTIFACT_BYTES:
            raise PlatformValidationError("Integration artifact must be non-empty and at most 250 MB.")
        request_id = str(self._sdk.audit_context.request_id or secrets.token_hex(12))
        origin = f"inprocess:{self._sdk.integration_id}:{request_id}:{secrets.token_hex(8)}:{name}"
        record = materialize_artifact_bytes(
            tenant_id=self._sdk.tenant_id, project_id=target_project,
            origin_url=origin, body=body, name=name, content_type=content_type,
            max_bytes=MAX_ARTIFACT_BYTES, store=self._sdk._media.store,
            integration_id=self._sdk.integration_id,
        )
        return {
            "media_id": str(record["id"]), "url": f"managed://{record['id']}",
            "name": str(record.get("name") or name),
            "content_type": str(record.get("content_type") or content_type),
            "project_id": target_project,
        }

    def delete(self, media_id: str, *, confirm_media_id: str) -> dict[str, Any]:
        self._allow("media:delete", project_id=self._sdk.project_id)
        return self._sdk._media.delete(media_id, confirm_media_id=confirm_media_id)

    def read_managed(self, media_id: str, *, max_bytes: int = MANAGED_READ_MAX_BYTES) -> bytes:
        """Read the durable bytes of a managed media record.

        The rest of this facade returns metadata only, deliberately. This is
        the one byte-returning method, and it exists so an operation can take a
        large document as a *reference* instead of inline JSON -- an inline
        document has to cross the 64 KB tool-input limit and a model's context
        to get here, which is the cost this platform exists to remove.

        Three things are bounded on purpose:

        - Only managed media is readable. A record whose bytes Lynewright never
          stored points at somebody else's URL; fetching it here would turn a
          media id into a server-side fetch primitive. Those go through
          ``IntegrationHTTP``, which validates the destination.
        - The path is resolved by ``resolve_managed_path``, which refuses
          anything that escapes the media root.
        - The size is checked before the read, not after, so an oversized file
          is refused rather than pulled into memory first.
        """
        # Imported here: integration_media_transfer imports this module.
        from app.services.integration_media_transfer import (
            IntegrationMediaTransferService,
            managed_media_root,
        )

        self._allow("media:read", project_id=self._sdk.project_id)
        limit = max(1, min(int(max_bytes), MANAGED_READ_MAX_BYTES))
        record = self._sdk._media.get(media_id)
        stored_as = str((record.get("metadata") or {}).get("stored_as") or "").strip()
        if not stored_as:
            raise PlatformValidationError(
                "Only managed media can be read; this record stores no durable bytes."
            )
        transfer = IntegrationMediaTransferService(temp_dir=managed_media_root())
        path = transfer.resolve_managed_path(stored_as)
        if not path.is_file():
            raise PlatformNotFoundError(f"Managed media '{media_id}' has no stored file.")
        size = path.stat().st_size
        if size > limit:
            raise PlatformValidationError(
                f"Managed media '{media_id}' is {size} bytes, over the {limit}-byte read limit."
            )
        return path.read_bytes()


class IntegrationCredentials(_Facade):
    def status(self, name: str) -> dict[str, Any]:
        self._allow("credentials:status")
        return self._sdk._credentials.get_status(name)

    def list(self) -> list[dict[str, Any]]:
        self._allow("credentials:list")
        return self._sdk._credentials.list_metadata()

    def set(self, *, name: str, secret: str, kind: str = "token") -> dict[str, Any]:
        self._allow("credentials:set")
        return self._sdk._credentials.set({"name": name, "secret": secret, "kind": kind})

    def use(self, name: str, operation: Callable[[str], _T]) -> _T:
        self._allow("credentials:use")
        def guarded(secret: str) -> _T:
            result = operation(secret)
            if _contains_secret(result, secret):
                raise PlatformOperationError("Credential operations must not return a raw secret.")
            return result
        return self._sdk._credentials.use(name, guarded)


class IntegrationAI(_Facade):
    def run(self, *, task: str, payload: dict[str, Any], project_id: str | None = None) -> dict[str, Any]:
        target_project = self._sdk._project(project_id)
        self._allow("ai:use", project_id=target_project)
        self._allow("jobs:create", project_id=target_project)
        return self._sdk._ai.run(task=task, payload=payload, jobs=self._sdk._jobs, project_id=target_project)


class IntegrationHTTP(_Facade):
    def validate_url(self, url: str) -> dict[str, Any]:
        self._allow("network:use")
        return self._sdk._http.validate_public_url(url)

    def get(self, url: str, *, max_bytes: int = 1_000_000, timeout_seconds: float = 15.0) -> dict[str, Any]:
        self._allow("network:use")
        return self._sdk._http.get(url, max_bytes=max_bytes, timeout_seconds=timeout_seconds)

    def request(
        self,
        url: str,
        *,
        method: str,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        json_body: Any | None = None,
        content: bytes | str | Iterable[bytes] | None = None,
        max_bytes: int = 1_000_000,
        timeout_seconds: float = 15.0,
        internal_endpoint_ref: str = "",
    ) -> dict[str, Any]:
        """Forwarded, not interpreted.

        The ref names a deployment-configured origin and buys access to that
        one origin; the outbound service re-derives it from the environment
        allowlist, so passing it through here grants nothing a provider could
        not already reach through its own connection profile.
        """
        self._allow("network:use")
        return self._sdk._http.request(
            url, method=method, headers=headers, params=params, json_body=json_body,
            content=content, max_bytes=max_bytes, timeout_seconds=timeout_seconds,
            internal_endpoint_ref=internal_endpoint_ref,
        )


class IntegrationSettings(_Facade):
    def get(self, key: str) -> dict[str, Any]:
        self._allow("settings:read")
        return self._sdk._settings.get(self._key(key))

    def set(self, key: str, value: Any) -> dict[str, Any]:
        self._allow("settings:write")
        return self._sdk._settings.set({"key": self._key(key), "value": value})

    def _key(self, key: str) -> str:
        if not _SETTING_KEY_RE.fullmatch(key):
            raise PlatformOperationError("Integration setting key is invalid.")
        return f"platform.integrations.{self._sdk.integration_id}.{key}"


class IntegrationEvents(_Facade):
    def publish(self, action: str, payload: dict[str, Any], *, project_id: str | None = None) -> dict[str, Any]:
        target_project = self._sdk._project(project_id)
        self._allow("events:publish", project_id=target_project)
        if _EVENT_ACTION_RE.fullmatch(action) is None:
            raise PlatformOperationError("Integration event action is invalid.")
        return self._sdk._events.publish({"type": f"integration.{self._sdk.integration_id}.{action}", "payload": payload, "project_id": target_project})


class IntegrationStorage(_Facade):
    def get(self, key: str) -> Any:
        self._allow("storage:read", project_id=self._sdk.project_id)
        return self._sdk._state.get_value(self._sdk._state_scope(), key)

    def set(self, key: str, value: Any) -> Any:
        self._allow("storage:write", project_id=self._sdk.project_id)
        return self._sdk._state.set_value(self._sdk._state_scope(), key, value)

    def delete(self, key: str) -> None:
        self._allow("storage:write", project_id=self._sdk.project_id)
        self._sdk._state.delete_value(self._sdk._state_scope(), key)

    def keys(self) -> list[str]:
        self._allow("storage:read", project_id=self._sdk.project_id)
        return self._sdk._state.list_values(self._sdk._state_scope())


class IntegrationMappings(_Facade):
    def get(self, external_type: str, external_id: str) -> dict[str, Any]:
        self._allow("mappings:read", project_id=self._sdk.project_id)
        return self._sdk._state.get_mapping(self._sdk._state_scope(), external_type, external_id)

    def put(self, *, external_type: str, external_id: str, lynewright_type: str, lynewright_id: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        self._allow("mappings:write", project_id=self._sdk.project_id)
        return self._sdk._state.put_mapping(self._sdk._state_scope(), external_type=external_type, external_id=external_id, lynewright_type=lynewright_type, lynewright_id=lynewright_id, metadata=metadata)

    def find_by_lynewright(self, lynewright_type: str, lynewright_id: str) -> dict[str, Any]:
        self._allow("mappings:read", project_id=self._sdk.project_id)
        return self._sdk._state.find_mapping_by_lynewright(self._sdk._state_scope(), lynewright_type, lynewright_id)


class IntegrationSyncState(_Facade):
    def get(self) -> dict[str, Any]:
        self._allow("sync:read", project_id=self._sdk.project_id)
        return self._sdk._state.get_sync_state(self._sdk._state_scope())

    def set(self, state: dict[str, Any]) -> dict[str, Any]:
        self._allow("sync:write", project_id=self._sdk.project_id)
        return self._sdk._state.set_sync_state(self._sdk._state_scope(), state)

    def checkpoint(self, *, cursor: str = "", etag: str = "", revision: str = "", incremental: bool = True) -> dict[str, Any]:
        self._allow("sync:write", project_id=self._sdk.project_id)
        return self._sdk._state.set_sync_checkpoint(self._sdk._state_scope(), cursor=cursor, etag=etag, revision=revision, incremental=incremental)


class IntegrationSDK:
    """Scoped facade set; it deliberately exposes no store, ORM, or secret getter."""

    def __init__(
        self,
        *,
        caller: CapabilityActor,
        integration: IntegrationActor,
        authorization: IntegrationAuthorizationService,
        tenant_id: str,
        project_id: str | None,
        connection_id: str | None,
        project_scope: ProjectScopePolicy | None,
        platform_policy: PlatformPolicy | None,
        audit_context: IntegrationAuditContext,
        projects: ProjectService,
        jobs: JobService,
        media: MediaService,
        ai: AIService,
        credentials: CredentialService,
        http: HTTPService,
        settings: SettingsService,
        events: EventService,
        state: IntegrationStateService,
        invocation_context: CapabilityInvocationContext | None = None,
        allow_disabled_integration_for_preview: bool = False,
    ) -> None:
        self.caller = caller
        self.integration = integration
        self.integration_id = integration.extension_id
        self.tenant_id = tenant_id
        self.project_id = project_id
        self.connection_id = connection_id
        self.audit_context = audit_context
        self.invocation_context = invocation_context
        self._allow_disabled_integration_for_preview = allow_disabled_integration_for_preview
        self._authorization = authorization
        self._project_scope = project_scope
        self._platform_policy = platform_policy
        self._projects, self._jobs, self._media, self._ai = projects, jobs, media, ai
        self._credentials, self._http, self._settings, self._events, self._state = credentials, http, settings, events, state
        self.logger = IntegrationLogger(self)
        self.projects = IntegrationProjects(self)
        self.jobs = IntegrationJobs(self)
        self.media = IntegrationMedia(self)
        self.credentials = IntegrationCredentials(self)
        self.ai = IntegrationAI(self)
        self.http = IntegrationHTTP(self)
        self.settings = IntegrationSettings(self)
        self.events = IntegrationEvents(self)
        self.storage = IntegrationStorage(self)
        self.mappings = IntegrationMappings(self)
        self.sync_state = IntegrationSyncState(self)

    def _allow(self, capability: str, *, project_id: str | None = None) -> IntegrationActor:
        return self._authorization.require_effective_capability(
            self.caller,
            self.integration_id,
            capability,
            project_id=project_id,
            project_scope=self._project_scope,
            platform_policy=self._platform_policy,
            allow_disabled_integration_for_preview=self._allow_disabled_integration_for_preview,
        )

    def _project(self, requested: str | None) -> str | None:
        if requested is not None and self.project_id is not None and requested != self.project_id:
            raise PlatformOperationError("Integration SDK cannot operate outside its bound project scope.")
        return requested if requested is not None else self.project_id

    def _state_scope(self) -> dict[str, str]:
        return {"integration_id": self.integration_id, "connection_id": self.connection_id or "", "project_id": self.project_id or ""}


class IntegrationSDKFactory:
    def __init__(
        self,
        store: PlatformStore | None = None,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        authorization: IntegrationAuthorizationService | None = None,
        connections: ConnectionProfileService | None = None,
        project_scope: ProjectScopePolicy | None = None,
        platform_policy: PlatformPolicy | None = None,
        http: HTTPService | None = None,
    ) -> None:
        self.store = store or PlatformStore()
        self.tenant_id = tenant_id
        self.authorization = authorization or IntegrationAuthorizationService(IntegrationRegistry(self.store, tenant_id=tenant_id))
        self.connections = connections
        # Project-scoped SDK calls must always carry an explicit host-owned
        # scope check.  Older callers supplied a callback in tests, but the
        # production factory was constructed without one, which meant a
        # connectionless project-scoped integration failed only when it first
        # touched storage/jobs/media.  Keep custom policy injection for
        # deployments that need stricter actor rules, while making the safe
        # tenant/project membership check the generic default for every
        # current and future integration type.
        self.project_scope = project_scope or self._default_project_scope
        self.platform_policy = platform_policy
        self.http = http or HTTPService()

    def _default_project_scope(
        self, _caller: CapabilityActor, _integration: IntegrationActor, project_id: str,
    ) -> bool:
        """Allow only active projects owned by this SDK factory's tenant.

        Capability intersection, integration grants, and any supplied custom
        policy are still enforced by ``ExtensionAuthorizationService``.  This
        default only establishes the missing host boundary and deliberately
        does not infer project access from provider metadata or credentials.
        """
        with self.store.connection() as db:
            row = db.execute(
                """SELECT 1 FROM tenant_projects
                   WHERE tenant_id=? AND project_id=? AND status='active'""",
                (self.tenant_id, str(project_id)),
            ).fetchone()
        return row is not None

    def create(
        self,
        *,
        caller: CapabilityActor,
        integration_id: str,
        connection_id: str | None = None,
        project_id: str | None = None,
        request_id: str = "",
        invocation_context: CapabilityInvocationContext | None = None,
        allow_disabled_integration_for_preview: bool = False,
    ) -> IntegrationSDK:
        if invocation_context is not None:
            if invocation_context.tenant_id != self.tenant_id:
                raise PlatformOperationError("Integration SDK context belongs to a different tenant.")
            if invocation_context.integration_id not in {None, integration_id}:
                raise PlatformOperationError("Integration SDK context belongs to a different integration.")
            if invocation_context.connection_id not in {None, connection_id}:
                raise PlatformOperationError("Integration SDK context belongs to a different connection.")
            if invocation_context.project_id not in {None, project_id}:
                raise PlatformOperationError("Integration SDK context belongs to a different project.")
            project_id = invocation_context.project_id or project_id
            request_id = request_id or invocation_context.trace_id
        integration = self.authorization.resolve_actor(integration_id)
        preview_exception = (
            allow_disabled_integration_for_preview
            and invocation_context is not None
            and invocation_context.entry_point == "integration-management.mapping-preview"
        )
        if not integration.enabled:
            if not preview_exception:
                raise PlatformOperationError("Integration is disabled.")
        if connection_id is not None:
            if self.connections is None:
                raise PlatformOperationError("Integration connection profiles are not configured for this SDK factory.")
            connection = self.connections.get(integration_id, connection_id)
            if not connection["enabled"]:
                raise PlatformOperationError("Integration connection is disabled.")
            connection_project = connection["project_id"]
            if connection_project and project_id and connection_project != project_id:
                raise PlatformOperationError("Integration connection is scoped to a different project.")
            project_id = connection_project or project_id
        audit_context = IntegrationAuditContext(
            caller.client_id, integration_id, connection_id, project_id, request_id,
            trace_id=invocation_context.trace_id if invocation_context is not None else "",
        )
        events = EventService(self.store, tenant_id=self.tenant_id)
        return IntegrationSDK(
            caller=caller, integration=integration, authorization=self.authorization, tenant_id=self.tenant_id,
            project_id=project_id, connection_id=connection_id, project_scope=self.project_scope,
            platform_policy=self.platform_policy, audit_context=audit_context,
            invocation_context=invocation_context,
            projects=ProjectService(self.store, tenant_id=self.tenant_id),
            jobs=JobService(self.store, tenant_id=self.tenant_id, event_bus=CoreEventBus(events)),
            media=MediaService(self.store, tenant_id=self.tenant_id), ai=AIService(),
            credentials=CredentialService(self.store), http=self.http, settings=SettingsService(self.store),
            events=events, state=IntegrationStateService(self.store, tenant_id=self.tenant_id),
            allow_disabled_integration_for_preview=allow_disabled_integration_for_preview,
        )


def _contains_secret(value: Any, secret: str) -> bool:
    if isinstance(value, str):
        return value == secret
    if isinstance(value, dict):
        return any(_contains_secret(nested, secret) for nested in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_contains_secret(nested, secret) for nested in value)
    return False
