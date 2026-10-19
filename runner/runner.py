"""Small HTTP-only host for one Lynewright native integration package."""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


class RunnerConfigurationError(RuntimeError):
    pass


def refuse_database_access() -> None:
    """Fail before importing a package if a Lynewright database/data mount is visible."""
    forbidden_env = ("LYNEWRIGHT_DB_PATH", "LYNEWRIGHT_DATABASE_URL", "LYNEWRIGHT_DATA_DIR", "LYNEWRIGHT_MANAGED_MEDIA_DIR")
    if any(os.environ.get(name) for name in forbidden_env):
        raise RunnerConfigurationError("Runner cannot start with Lynewright database or data environment configured.")
    for path in (
        Path("/data"), Path("/var/lib/lynewright"), Path("/app/backend/data"),
        Path("/lynewright/backend/data"), Path("/opt/lynewright/data"),
    ):
        if path.exists():
            raise RunnerConfigurationError("Runner cannot start while a Lynewright data directory is visible.")


def _manifest_from_module(module_name: str, integration_id: str) -> dict[str, Any]:
    module = importlib.import_module(module_name)
    candidates = [
        value for name, value in vars(module).items()
        if name.endswith("_MANIFEST") and isinstance(value, dict) and value.get("id") == integration_id
    ]
    if len(candidates) != 1:
        raise RunnerConfigurationError("Configured integration manifest is missing or ambiguous.")
    return candidates[0]


class RunnerSDK:
    """Minimal non-database SDK facade with API-mediated media and credentials."""

    def __init__(self, *, client: "RunnerClient", invocation: dict[str, Any]) -> None:
        self._client = client
        self._invocation = invocation
        self.tenant_id = str((invocation.get("context") or {}).get("tenant_id") or "")
        self.project_id = str((invocation.get("context") or {}).get("project_id") or "") or None
        self.integration_id = str(invocation.get("integration_id") or "")
        self.connection_id = str((invocation.get("context") or {}).get("connection_id") or "") or None
        self.settings = dict(invocation.get("settings") or {})
        self.credentials = _RunnerCredentials(invocation.get("credentials") or {})
        connection = invocation.get("connection") if isinstance(invocation.get("connection"), dict) else {}
        self.http = _RunnerHTTP(
            client,
            internal_endpoint_ref=str(connection.get("internal_endpoint_ref") or ""),
            internal_endpoint_url=str(connection.get("internal_endpoint_url") or ""),
            media_base_url=str(invocation.get("media_base_url") or client.base_url),
        )
        self.media = _RunnerMedia(client, invocation, self.http)
        self.logger = _RunnerLogger()
        self.caller = _RunnerCaller((invocation.get("kwargs") or {}).get("_lynewright_sdk_actor") or {})
        self.jobs = _DatabaseUnavailable("jobs")
        self.ai = _DatabaseUnavailable("AI provider access")
        self.events = _DatabaseUnavailable("event bus")
        self.storage = _DatabaseUnavailable("integration storage")
        self.mappings = _DatabaseUnavailable("integration mappings")
        self.sync_state = _DatabaseUnavailable("sync state")
        self.projects = _DatabaseUnavailable("project database access")


class _DatabaseUnavailable:
    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, _name: str) -> Any:
        raise RunnerConfigurationError(f"{self._name} requires the Lynewright API database and is unavailable in a runner.")


class _RunnerCredentials:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = {str(key): str(value) for key, value in values.items()}

    def get(self, name: str) -> str:
        try:
            return self._values[str(name)]
        except KeyError as exc:
            raise RunnerConfigurationError("Credential ref was not declared and bound for this operation.") from exc

    def use(self, name: str, operation: Any) -> Any:
        return operation(self.get(name))

    def list(self) -> list[str]:
        return sorted(self._values)


class _RunnerMedia:
    def __init__(self, client: "RunnerClient", invocation: dict[str, Any], http: "_RunnerHTTP") -> None:
        self._client = client
        self._invocation = invocation
        self._http = http

    def get_link(self, media_id: str, *, ttl_seconds: int = 900) -> str:
        request_id = str(self._invocation["id"])
        result = self._client.request(
            "GET", f"/api/runner/v1/invocations/{quote(request_id)}/media/{quote(str(media_id))}/link?ttl_seconds={int(ttl_seconds)}",
        )
        return str(result["url"])

    def upload_artifact(self, *, name: str, content_type: str, body: bytes) -> dict[str, Any]:
        request_id = str(self._invocation["id"])
        return self._client.request(
            "POST", f"/api/runner/v1/invocations/{quote(request_id)}/artifacts",
            body=body, headers={"Content-Type": content_type, "X-Filename": name},
        )

    def read_managed(self, media_id: str, *, max_bytes: int = 20_000_000) -> bytes:
        url = self.get_link(media_id)
        response = self._http.request(url, method="GET", max_bytes=max_bytes, timeout_seconds=20)
        body = response.get("body") if isinstance(response.get("body"), bytes) else b""
        if len(body) > max_bytes:
            raise RunnerConfigurationError("Managed media exceeds the runner read limit.")
        return body


class _RunnerHTTP:
    """Use Lynewright's SSRF-pinned HTTP boundary without a database facade."""

    def __init__(
        self, client: "RunnerClient", *, internal_endpoint_ref: str = "",
        internal_endpoint_url: str = "", media_base_url: str = "",
    ) -> None:
        self._client = client
        self._internal_endpoint_ref = internal_endpoint_ref
        self._internal_endpoint_url = internal_endpoint_url
        self._media_base_url = media_base_url.rstrip("/")

    def request(
        self, url: str, *, method: str = "GET", headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None, json_body: Any | None = None,
        content: bytes | str | None = None, max_bytes: int = 1_000_000,
        timeout_seconds: float = 15.0, internal_endpoint_ref: str = "",
    ) -> dict[str, Any]:
        parsed = urlsplit(url)
        selected_ref = internal_endpoint_ref
        environment_name = ""
        environment_value = ""
        original_value: str | None = None
        if selected_ref:
            if selected_ref != self._internal_endpoint_ref or not self._internal_endpoint_url:
                raise RunnerConfigurationError("Runner HTTP cannot use an undeclared internal endpoint reference.")
            environment_name = f"LYNEWRIGHT_INTERNAL_ENDPOINT__{selected_ref.upper().replace('-', '_')}"
            environment_value = self._internal_endpoint_url
        elif parsed.path.startswith("/api/media/l/"):
            request_origin = f"{parsed.scheme}://{parsed.netloc}"
            media = urlsplit(self._media_base_url)
            media_origin = f"{media.scheme}://{media.netloc}"
            if request_origin != media_origin:
                raise RunnerConfigurationError("Runner media links must come from its configured Lynewright API.")
            selected_ref = "media"
            environment_name = "LYNEWRIGHT_INTERNAL_MEDIA_URL"
            environment_value = self._media_base_url
        if selected_ref:
            original_value = os.environ.get(environment_name)
            os.environ[environment_name] = environment_value
        from app.services.platform_services import HTTPService
        try:
            return HTTPService().request(
                url, method=method, headers=headers, params=params, json_body=json_body,
                content=content, max_bytes=max_bytes, timeout_seconds=timeout_seconds,
                internal_endpoint_ref=selected_ref,
            )
        finally:
            if selected_ref:
                if original_value is None:
                    os.environ.pop(environment_name, None)
                else:
                    os.environ[environment_name] = original_value

    def validate_url(self, url: str) -> dict[str, Any]:
        from app.services.platform_services import HTTPService

        return HTTPService().validate_public_url(url)

    def get(
        self, url: str, *, max_bytes: int = 1_000_000, timeout_seconds: float = 15.0,
    ) -> dict[str, Any]:
        return self.request(url, method="GET", max_bytes=max_bytes, timeout_seconds=timeout_seconds)


class _RunnerLogger:
    def write(self, level: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        # Keep provider-controlled text and fields off runner stdout/stderr.
        return

    def info(self, _message: str, **_fields: Any) -> None:
        return

    warning = info
    error = info


class _RunnerCaller:
    def __init__(self, value: dict[str, Any]) -> None:
        self.client_id = str(value.get("client_id") or "container-runner")
        self.name = str(value.get("name") or "Container runner")
        self.client_type = str(value.get("client_type") or "integration-agent")
        self.permission_mode = str(value.get("permission_mode") or "restricted")
        self.granted_capabilities = frozenset(value.get("granted_capabilities") or [])
        self.enabled = bool(value.get("enabled", True))
        self.internal = False


class RunnerClient:
    def __init__(self, base_url: str, token: str, timeout: float = 40.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(
        self, method: str, path: str, *, body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        payload = body
        request_headers = {"Authorization": f"Bearer {self.token}", **(headers or {})}
        if isinstance(body, (dict, list)):
            payload = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        request = Request(self.base_url + path, data=payload, method=method, headers=request_headers)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                result = response.read(30_000_001)
        except (HTTPError, URLError, TimeoutError) as exc:
            # Do not propagate response bodies; a provider error could contain credentials.
            raise RunnerConfigurationError("Runner API request failed.") from exc
        if not result:
            return {}
        try:
            return json.loads(result.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise RunnerConfigurationError("Runner API returned an invalid response.") from None


class _CaptureContext:
    def __init__(self, integration_id: str) -> None:
        self.integration_id = integration_id
        self.configuration: dict[str, Any] = {}
        self.items: dict[str, tuple[dict[str, Any], Any]] = {}

    def contribute(self, *, local_id: str, metadata: dict[str, Any], handler: Any, **_kwargs: Any) -> None:
        self.items[local_id] = (dict(metadata), handler)


def compile_handlers(manifest: dict[str, Any]) -> dict[str, tuple[dict[str, Any], Any]]:
    from app.services.integration_definition_compiler import IntegrationDefinitionCompiler

    implementation_refs = [
        str(item.get("implementation") or "")
        for item in manifest.get("provides", []) if isinstance(item, dict)
    ]
    roots = tuple(sorted({value.partition(":")[0].split(".", 1)[0] for value in implementation_refs if value}))
    if not roots:
        raise RunnerConfigurationError("The selected package declares no native implementation.")
    compiled = IntegrationDefinitionCompiler(allowed_roots=roots, integrations=object()).compile(
        {"id": manifest["id"], "manifest": manifest},
    )
    if not compiled.ok or compiled.definition is None:
        raise RunnerConfigurationError("Integration package could not be compiled by the Lynewright compiler.")
    context = _CaptureContext(str(manifest["id"]))
    compiled.definition.activate(context)
    return context.items


def _execute(invocation: dict[str, Any], handlers: dict[str, tuple[dict[str, Any], Any]], client: RunnerClient) -> Any:
    contribution_id = str(invocation.get("contribution_id") or "")
    prefix = str(invocation.get("integration_id") or "") + ":"
    local_id = contribution_id.removeprefix(prefix) if contribution_id.startswith(prefix) else ""
    try:
        metadata, handler = handlers[local_id]
    except KeyError as exc:
        raise RunnerConfigurationError("Claimed operation is not declared by this integration image.") from exc
    args = list(invocation.get("args") or [])
    kwargs = dict(invocation.get("kwargs") or {})
    operation = str(invocation.get("operation") or "")
    payload: dict[str, Any] = {}
    for value in args:
        if isinstance(value, str) and value == operation:
            continue
        if isinstance(value, dict):
            payload = value
            break
    payload.update({key: value for key, value in kwargs.items() if key != "_lynewright_sdk_actor"})
    sdk = RunnerSDK(client=client, invocation=invocation)
    from app.services.integration_worker_invocations import context_from_dict
    invocation_context = context_from_dict(invocation.get("context") or {})
    if str(metadata.get("invocation") or "direct") == "sdk_scoped":
        return handler(sdk, operation, payload, invocation_context=invocation_context)
    return handler(operation, payload, invocation_context=invocation_context)


def run() -> None:
    refuse_database_access()
    api_url = os.environ.get("LYNEWRIGHT_RUNNER_API_URL", "").strip()
    token = os.environ.get("LYNEWRIGHT_RUNNER_TOKEN", "").strip()
    integration_id = os.environ.get("LYNEWRIGHT_RUNNER_INTEGRATION_ID", "").strip()
    manifest_module = os.environ.get("LYNEWRIGHT_RUNNER_MANIFEST", "").strip()
    if not all((api_url, token, integration_id, manifest_module)):
        raise RunnerConfigurationError("Runner requires API URL, token, integration id, and manifest module settings.")
    manifest = _manifest_from_module(manifest_module, integration_id)
    handlers = compile_handlers(manifest)
    client = RunnerClient(api_url, token)
    last_heartbeat = 0.0
    while True:
        try:
            if time.monotonic() - last_heartbeat > 10:
                client.request("POST", "/api/runner/v1/heartbeat", body={})
                last_heartbeat = time.monotonic()
            response = client.request("POST", "/api/runner/v1/claim", body={"wait_seconds": 15})
            invocation = response.get("invocation") if isinstance(response, dict) else None
            if not invocation:
                continue
            try:
                result = _execute(invocation, handlers, client)
                client.request(
                    "POST", f"/api/runner/v1/{quote(str(invocation['id']))}/complete",
                    body={"claim_token": invocation["claim_token"], "result": result},
                )
            except Exception as exc:  # Never transmit exception text or credentials.
                client.request(
                    "POST", f"/api/runner/v1/{quote(str(invocation['id']))}/fail",
                    body={"claim_token": invocation["claim_token"], "error_code": type(exc).__name__},
                )
        except RunnerConfigurationError:
            # Keep provider values and remote error details out of stdout/stderr.
            time.sleep(2)


if __name__ == "__main__":
    try:
        run()
    except RunnerConfigurationError as exc:
        message = str(exc)
        if message == "Runner cannot start with Lynewright database or data environment configured." or message == "Runner cannot start while a Lynewright data directory is visible.":
            print("Lynewright runner refused: database or data environment configuration is present", file=sys.stderr)
        else:
            print(f"Lynewright runner stopped: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as exc:
        print(f"Lynewright runner stopped: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
