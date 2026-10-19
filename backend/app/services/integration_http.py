"""Provider-neutral HTTP adapter primitives built on connection profiles."""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any, Literal
from urllib.parse import urlsplit

from app.services.integration_connections import ConnectionProfileService
from app.services.integration_errors import normalize_external_error, normalize_http_error
from app.services.integration_sdk import IntegrationSDK
from app.services.platform_services import PlatformOperationError, PlatformValidationError


AuthType = Literal["none", "bearer", "authorization_header", "api_key_header", "basic"]
_PATH_RE = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/?=-]*$")
_SENSITIVE_HEADER = re.compile(r"(authorization|cookie|token|secret|api[_-]?key|password)", re.IGNORECASE)


class IntegrationHTTPAdapter:
    """Turns a generic connection profile into bounded safe HTTP operations."""

    def __init__(self, connections: ConnectionProfileService) -> None:
        self.connections = connections

    def request(
        self,
        sdk: IntegrationSDK,
        *,
        connection_id: str | None = None,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        json_body: Any | None = None,
        content: bytes | str | Iterable[bytes] | None = None,
        auth: AuthType = "none",
        credential_ref: str | None = None,
        api_key_header: str = "X-Api-Key",
        max_bytes: int = 1_000_000,
        timeout_seconds: float = 15.0,
    ) -> dict[str, Any]:
        connection = self._connection(sdk, connection_id)
        url = self._url(str(connection["endpoint_url"]), path)
        base_headers = dict(headers or {})
        # Carried from the connection, never from the caller. The outbound
        # service re-resolves it against the deployment allowlist, so this
        # selects a pre-approved origin rather than asserting one.
        endpoint_ref = str(connection.get("internal_endpoint_ref") or "")             if str(connection.get("origin_kind") or "public") == "internal_endpoint" else ""

        def send(bound_headers: dict[str, str], *, secrets: tuple[str, ...] = ()) -> dict[str, Any]:
            try:
                # Passed only when the connection actually names one, so the
                # public path's call shape is unchanged.
                extra = {"internal_endpoint_ref": endpoint_ref} if endpoint_ref else {}
                response = sdk.http.request(
                    url, method=method, headers=bound_headers, params=params, json_body=json_body,
                    content=content, max_bytes=max_bytes, timeout_seconds=timeout_seconds,
                    **extra,
                )
            except Exception as exc:
                raise normalize_external_error(exc, secrets=secrets) from exc
            return self._response(response)

        selected_credential = credential_ref or (connection["credential_refs"][0] if connection["credential_refs"] else "")
        if auth == "none":
            return send(base_headers)
        if not selected_credential:
            raise PlatformValidationError("This HTTP authentication mode requires a credential reference.")
        if auth not in {"bearer", "authorization_header", "api_key_header", "basic"}:
            raise PlatformValidationError("Unsupported generic HTTP authentication mode.")

        def with_credential(secret: str) -> dict[str, Any]:
            bound = dict(base_headers)
            if auth == "bearer":
                bound["Authorization"] = f"Bearer {secret}"
            elif auth == "authorization_header":
                bound["Authorization"] = secret
            elif auth == "api_key_header":
                if not re.fullmatch(r"[A-Za-z0-9-]{1,120}", api_key_header):
                    raise PlatformValidationError("API key header name is invalid.")
                bound[api_key_header] = secret
            else:
                # A Basic credential reference stores the already encoded user:password token.
                bound["Authorization"] = f"Basic {secret}"
            return send(bound, secrets=(secret,))

        return sdk.credentials.use(selected_credential, with_credential)

    def request_json(self, sdk: IntegrationSDK, **kwargs: Any) -> Any:
        response = self.request(sdk, **kwargs)
        try:
            return json.loads(response["body"].decode("utf-8"))
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise normalize_external_error(exc) from exc

    @staticmethod
    def require_success(response: dict[str, Any], *, provider: str = "") -> dict[str, Any]:
        """Return a successful response or raise a normalized provider error."""
        status_code = int(response["status_code"])
        if not 200 <= status_code < 300:
            raise normalize_http_error(status_code, headers=response.get("headers"), provider=provider)
        return response

    def health(
        self,
        sdk: IntegrationSDK,
        *,
        connection_id: str | None = None,
        path: str = "/health",
        auth: AuthType = "none",
        credential_ref: str | None = None,
    ) -> dict[str, Any]:
        selected_connection = connection_id or sdk.connection_id
        if not selected_connection:
            raise PlatformOperationError("A connection profile is required for an HTTP health check.")
        try:
            response = self.request(sdk, connection_id=selected_connection, method="GET", path=path, auth=auth, credential_ref=credential_ref)
            status = int(response["status_code"])
            state = "HEALTHY" if 200 <= status < 400 else "DEGRADED"
            result = self.connections.record_health(
                sdk.integration_id, selected_connection, state=state, latency_ms=None,
                error_code="" if state == "HEALTHY" else f"http.{status}",
            )
            return {"state": state, "response": response, "connection": result}
        except Exception as exc:
            result = self.connections.record_health(
                sdk.integration_id, selected_connection, state="UNAVAILABLE", error_code=type(exc).__name__.lower(),
            )
            return {"state": "UNAVAILABLE", "error": type(exc).__name__, "connection": result}

    def _connection(self, sdk: IntegrationSDK, connection_id: str | None) -> dict[str, Any]:
        selected = connection_id or sdk.connection_id
        if not selected:
            raise PlatformOperationError("A connection profile is required for an HTTP adapter request.")
        connection = self.connections.get(sdk.integration_id, selected)
        if not connection["enabled"]:
            raise PlatformOperationError("Integration connection is disabled.")
        if not connection["endpoint_url"]:
            raise PlatformValidationError("Integration connection does not define an endpoint URL.")
        return connection

    @staticmethod
    def _url(endpoint: str, path: str) -> str:
        if _PATH_RE.fullmatch(path) is None:
            raise PlatformValidationError("HTTP adapter paths must be absolute relative paths without an authority.")
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc or path.startswith("//"):
            raise PlatformValidationError("HTTP adapter path cannot replace the approved connection origin.")
        return endpoint.rstrip("/") + path

    @staticmethod
    def _response(response: dict[str, Any]) -> dict[str, Any]:
        headers = {
            str(key).lower(): str(value) for key, value in dict(response.get("headers") or {}).items()
            if _SENSITIVE_HEADER.search(str(key)) is None
        }
        rate_limit = {
            key: headers[key] for key in ("retry-after", "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset")
            if key in headers
        }
        return {
            "status_code": int(response["status_code"]),
            "content_type": str(response.get("content_type") or ""),
            "headers": headers,
            "body": response.get("body") or b"",
            "rate_limit": rate_limit,
        }
