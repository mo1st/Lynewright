"""Generic bridges for integration jobs, events, and scheduled polling."""
from __future__ import annotations

import time
from typing import Any, Callable

from app.services.integration_contributions import Contribution, ContributionRegistry
from app.services.integration_jobs import IntegrationJobLifecycleService
from app.services.integration_sdk import IntegrationSDK, IntegrationSDKFactory
from app.services.integration_state import IntegrationScheduleStateService
from app.services.platform_capabilities import CapabilityActor
from app.services.platform_services import PlatformOperationError, PlatformValidationError


class IntegrationAutomationBridge:
    def __init__(
        self,
        contributions: ContributionRegistry,
        sdk_factory: IntegrationSDKFactory,
        *,
        now: Callable[[], float] = time.time,
        schedule_state: IntegrationScheduleStateService | None = None,
        jobs: IntegrationJobLifecycleService | None = None,
    ) -> None:
        self.contributions, self.sdk_factory, self.now = contributions, sdk_factory, now
        self.schedule_state = schedule_state or IntegrationScheduleStateService(
            sdk_factory.store,
            tenant_id=sdk_factory.tenant_id,
        )
        self.jobs = jobs or IntegrationJobLifecycleService(sdk_factory.store, tenant_id=sdk_factory.tenant_id)

    def start_remote_operation(self, sdk: IntegrationSDK, contribution_id: str, *, job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        contribution = self._contribution(contribution_id, "job")
        job = sdk.jobs.create({"type": job_type, "payload": payload})
        self.jobs.register(
            job_id=job["id"],
            integration_id=contribution.integration_id,
            connection_id=contribution.connection_id,
            contribution_id=contribution.id,
            policy=str(contribution.metadata.get("active_job_policy") or "drain"),
        )
        sdk.jobs.start(job["id"])
        try:
            value = self.contributions.invoke(contribution.id, sdk, job, payload)
        except Exception as exc:
            sdk.jobs.fail(job["id"], code="integration.remote_start", message=type(exc).__name__)
            raise PlatformOperationError("Integration remote operation could not be started.") from exc
        return {"job": sdk.jobs.get(job["id"]), "remote": value}

    def apply_callback(self, sdk: IntegrationSDK, *, job_id: str, status: str, progress: int | None = None, result: dict[str, Any] | None = None, error: dict[str, str] | None = None) -> dict[str, Any]:
        if not self.jobs.callback_allowed(job_id=job_id, integration_id=sdk.integration_id, connection_id=sdk.connection_id):
            return {"id": job_id, "ignored": True, "reason": "cancelled_or_terminal"}
        if status == "progress":
            if progress is None:
                raise PlatformValidationError("Job progress callbacks require progress.")
            return self.jobs.jobs.report_progress(job_id, progress=progress)
        if status == "completed":
            return self.jobs.jobs.complete(job_id, result=result)
        if status == "failed":
            return self.jobs.jobs.fail(job_id, code=str((error or {}).get("code") or "integration.remote"), message=str((error or {}).get("message") or "Remote operation failed."))
        raise PlatformValidationError("Unsupported integration callback job status.")

    def retry_remote_operation(self, sdk: IntegrationSDK, job_id: str) -> dict[str, Any]:
        """Retry an owned remote operation once its integration is available again."""
        owner = self.jobs.owner(job_id)
        if owner["integration_id"] != sdk.integration_id or owner["connection_id"] != (sdk.connection_id or ""):
            raise PlatformOperationError("Integration retry does not own this job.")
        contribution = self._contribution(owner["contribution_id"], "job")
        job = sdk.jobs.retry(job_id)
        self.jobs.reopen(job_id=job_id, integration_id=sdk.integration_id, connection_id=sdk.connection_id)
        sdk.jobs.start(job_id)
        try:
            value = self.contributions.invoke(contribution.id, sdk, job, dict(job["payload"]))
        except Exception as exc:
            sdk.jobs.fail(job_id, code="integration.remote_retry", message=type(exc).__name__)
            raise PlatformOperationError("Integration remote operation could not be retried.") from exc
        return {"job": self.jobs.jobs.get(job_id), "remote": value}

    def dispatch_event(self, actor: CapabilityActor, *, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        delivered, failures = 0, []
        for contribution in self.contributions.list(kind="event"):
            if not self._matches(str(contribution.metadata.get("pattern") or ""), event_type):
                continue
            try:
                sdk = self._sdk(actor, contribution)
                self.contributions.invoke(contribution.id, sdk, {"type": event_type, "payload": payload})
                delivered += 1
            except Exception:
                failures.append(contribution.id)
        return {"delivered": delivered, "failures": failures}

    def run_scheduled(self, actor: CapabilityActor) -> dict[str, Any]:
        ran, failures = [], []
        now = self.now()
        for contribution in self.contributions.list(kind="scheduled"):
            interval = contribution.metadata.get("interval_seconds")
            if not isinstance(interval, int) or not 1 <= interval <= 86_400:
                failures.append(contribution.id)
                continue
            checkpoint = self.schedule_state.get(
                integration_id=contribution.integration_id,
                connection_id=contribution.connection_id,
                contribution_id=contribution.id,
            )
            if checkpoint is not None and now < checkpoint["next_due_at_epoch"]:
                continue
            self.schedule_state.checkpoint(
                integration_id=contribution.integration_id,
                connection_id=contribution.connection_id,
                contribution_id=contribution.id,
                next_due_at_epoch=now + interval,
                last_run_at_epoch=now,
            )
            try:
                self.contributions.invoke(contribution.id, self._sdk(actor, contribution))
                ran.append(contribution.id)
            except Exception:
                failures.append(contribution.id)
        return {"ran": ran, "failures": failures}

    def _sdk(self, actor: CapabilityActor, contribution: Contribution) -> IntegrationSDK:
        return self.sdk_factory.create(caller=actor, integration_id=contribution.integration_id, connection_id=contribution.connection_id)

    def _contribution(self, contribution_id: str, kind: str) -> Contribution:
        value = self.contributions.get(contribution_id)
        if value.kind != kind:
            raise PlatformValidationError(f"Integration contribution is not a {kind} contribution.")
        return value

    @staticmethod
    def _matches(pattern: str, event_type: str) -> bool:
        return pattern == "*" or pattern == event_type or (pattern.endswith(".*") and event_type.startswith(pattern[:-1]))
