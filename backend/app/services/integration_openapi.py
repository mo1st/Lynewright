"""Small OpenAPI 3.x helper over the connection-bound safe HTTP adapter."""
from __future__ import annotations

import json
import re
import secrets
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote

import yaml

from app.services.integration_errors import normalize_external_error
from app.services.integration_http import AuthType, IntegrationHTTPAdapter
from app.services.integration_sdk import IntegrationSDK
from app.services.platform_services import PlatformValidationError


_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head"})
_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,120}$")
_SENSITIVE_HEADER = re.compile(r"^(authorization|cookie|x-api-key)$", re.IGNORECASE)
MAX_DOCUMENT_BYTES = 1_000_000


@dataclass(frozen=True, slots=True)
class OpenAPIOperation:
    operation_id: str
    method: str
    path: str
    value: dict[str, Any]


class IntegrationOpenAPIClient:
    """Supports the small, repeatable OpenAPI subset useful to an adapter."""

    def __init__(self, document: dict[str, Any] | str | bytes, http: IntegrationHTTPAdapter) -> None:
        self.document = self._document(document)
        self.http = http
        self._operations = self._operations_from_document()

    def execute(
        self,
        sdk: IntegrationSDK,
        operation_id: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        connection_id: str | None = None,
        auth: AuthType | Literal["auto"] = "auto",
        credential_ref: str | None = None,
        idempotency_key: str | None = None,
        max_bytes: int = 1_000_000,
        timeout_seconds: float = 15.0,
        include_response_metadata: bool = False,
    ) -> Any:
        operation = self.operation(operation_id)
        args = dict(arguments or {})
        path_values = self._section(args, "path")
        query_values = self._section(args, "query")
        header_values = self._section(args, "headers")
        path, query, headers = self._parameters(operation, path_values, query_values, header_values)
        if idempotency_key is not None:
            if not 8 <= len(idempotency_key) <= 128:
                raise PlatformValidationError("OpenAPI idempotency keys must be 8-128 characters.")
            headers["Idempotency-Key"] = idempotency_key
        body = args.get("body")
        self._validate_request_body(operation, body)
        selected_auth, api_key_header = self._auth(operation, auth)
        request_body = self._resolve(operation.value.get("requestBody")) if operation.value.get("requestBody") is not None else None
        request_content = request_body.get("content") if isinstance(request_body, dict) else None
        json_body: Any | None = None
        content: bytes | str | Iterable[bytes] | None = None
        if body is not None:
            if not isinstance(request_content, dict):
                raise PlatformValidationError("OpenAPI request body content is invalid.")
            if "multipart/form-data" in request_content:
                boundary = "lynewright-" + secrets.token_hex(18)
                content = self._multipart_body(body, boundary)
                headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
            elif "application/octet-stream" in request_content:
                from app.services.integration_media_transfer import ManagedMediaUpload

                if not isinstance(body, ManagedMediaUpload):
                    raise PlatformValidationError("OpenAPI binary request bodies require a managed media upload.")
                content = body.iter_chunks()
                headers["Content-Type"] = "application/octet-stream"
            elif "application/json" in request_content:
                json_body = body
            else:
                raise PlatformValidationError("OpenAPI helper supports JSON, multipart/form-data, and binary request bodies only.")
        response = self.http.request(
            sdk,
            connection_id=connection_id,
            method=operation.method.upper(),
            path=path,
            headers=headers,
            params=query or None,
            json_body=json_body,
            content=content,
            auth=selected_auth,
            credential_ref=credential_ref,
            api_key_header=api_key_header,
            max_bytes=max_bytes,
            timeout_seconds=timeout_seconds,
        )
        self.http.require_success(response, provider="openapi")
        if not response["body"]:
            return {"status_code": int(response["status_code"]), "value": None} if include_response_metadata else None
        try:
            value = json.loads(response["body"].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise normalize_external_error(exc, provider="openapi") from exc
        try:
            self._validate_response(operation, int(response["status_code"]), value)
        except PlatformValidationError as exc:
            raise normalize_external_error(ValueError(str(exc)), provider="openapi") from exc
        return {"status_code": int(response["status_code"]), "value": value} if include_response_metadata else value

    def operation(self, operation_id: str) -> OpenAPIOperation:
        try:
            return self._operations[operation_id]
        except KeyError as exc:
            raise PlatformValidationError("OpenAPI operationId is not defined.") from exc

    def operations(self) -> list[OpenAPIOperation]:
        """Return a deterministic, read-only operation directory for management UI."""
        return [self._operations[key] for key in sorted(self._operations)]

    def _operations_from_document(self) -> dict[str, OpenAPIOperation]:
        paths = self.document.get("paths")
        if not isinstance(paths, dict):
            raise PlatformValidationError("OpenAPI document must contain an object paths field.")
        operations: dict[str, OpenAPIOperation] = {}
        for path, path_item in paths.items():
            if not isinstance(path, str) or not path.startswith("/") or not isinstance(path_item, dict):
                raise PlatformValidationError("OpenAPI paths must be relative path objects.")
            for method, value in path_item.items():
                if str(method).lower() not in _METHODS:
                    continue
                if not isinstance(value, dict) or not isinstance(value.get("operationId"), str):
                    raise PlatformValidationError("OpenAPI operations require a unique operationId.")
                operation_id = str(value["operationId"])
                if operation_id in operations:
                    raise PlatformValidationError("OpenAPI operationId values must be unique.")
                operations[operation_id] = OpenAPIOperation(operation_id, str(method).lower(), path, value)
        if not operations:
            raise PlatformValidationError("OpenAPI document defines no supported operations.")
        return operations

    def _parameters(self, operation: OpenAPIOperation, path_values: dict[str, Any], query_values: dict[str, Any], header_values: dict[str, Any]) -> tuple[str, dict[str, str], dict[str, str]]:
        path = operation.path
        query: dict[str, str] = {}
        headers: dict[str, str] = {}
        if any(_HEADER_NAME.fullmatch(str(name)) is None or _SENSITIVE_HEADER.fullmatch(str(name)) for name in header_values):
            raise PlatformValidationError("OpenAPI cannot supply sensitive or invalid headers directly.")
        path_item = self.document["paths"][operation.path]
        parameters = [*self._parameter_list(path_item.get("parameters")), *self._parameter_list(operation.value.get("parameters"))]
        seen: set[tuple[str, str]] = set()
        for parameter in parameters:
            parameter = self._resolve(parameter)
            if not isinstance(parameter, dict):
                raise PlatformValidationError("OpenAPI parameter is invalid.")
            name, location = str(parameter.get("name") or ""), str(parameter.get("in") or "")
            if not name or location not in {"path", "query", "header"} or (name, location) in seen:
                raise PlatformValidationError("OpenAPI parameters must have unique name and supported location.")
            seen.add((name, location))
            source = {"path": path_values, "query": query_values, "header": header_values}[location]
            present = name in source and source[name] is not None
            if bool(parameter.get("required")) and not present:
                raise PlatformValidationError(f"OpenAPI operation requires {location} parameter {name}.")
            if not present:
                continue
            value = source[name]
            self._validate_schema(parameter.get("schema") or {}, value, f"{location}.{name}")
            text = self._scalar(value, f"{location}.{name}")
            if location == "path":
                path = path.replace("{" + name + "}", quote(text, safe=""))
            elif location == "query":
                query[name] = text
            else:
                if _HEADER_NAME.fullmatch(name) is None or _SENSITIVE_HEADER.fullmatch(name):
                    raise PlatformValidationError("OpenAPI cannot supply sensitive or invalid headers directly.")
                headers[name] = text
        if "{" in path or "}" in path:
            raise PlatformValidationError("OpenAPI path parameters are incomplete.")
        return path, query, headers

    def _validate_request_body(self, operation: OpenAPIOperation, body: Any) -> None:
        request_body = operation.value.get("requestBody")
        if request_body is None:
            if body is not None:
                raise PlatformValidationError("OpenAPI operation does not accept a request body.")
            return
        resolved = self._resolve(request_body)
        if not isinstance(resolved, dict):
            raise PlatformValidationError("OpenAPI requestBody must be an object.")
        if bool(resolved.get("required")) and body is None:
            raise PlatformValidationError("OpenAPI operation requires a request body.")
        if body is None:
            return
        content = resolved.get("content")
        if not isinstance(content, dict):
            raise PlatformValidationError("OpenAPI request body has no supported content declaration.")
        selected_type = next((
            candidate for candidate in ("multipart/form-data", "application/octet-stream", "application/json")
            if candidate in content
        ), "")
        media = content.get(selected_type) if selected_type else None
        if not isinstance(media, dict) or not isinstance(media.get("schema"), dict):
            raise PlatformValidationError("OpenAPI helper supports JSON, multipart/form-data, and binary request bodies only.")
        if selected_type == "application/octet-stream":
            from app.services.integration_media_transfer import ManagedMediaUpload

            if not isinstance(body, ManagedMediaUpload):
                raise PlatformValidationError("OpenAPI binary request bodies require a managed media upload.")
            return
        self._validate_schema(media["schema"], body, "body")

    @staticmethod
    def _multipart_body(body: Any, boundary: str) -> Iterator[bytes]:
        from app.services.integration_media_transfer import ManagedMediaUpload

        if not isinstance(body, dict):
            raise PlatformValidationError("OpenAPI multipart request bodies must be objects.")
        for raw_name, value in body.items():
            name = re.sub(r"[^A-Za-z0-9_.-]", "_", str(raw_name))[:120] or "field"
            if isinstance(value, ManagedMediaUpload):
                filename = str(value.name or "upload").replace("\\", "/").split("/")[-1]
                filename = re.sub(r"[^A-Za-z0-9_. -]", "_", filename)[:180] or "upload"
                yield f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\nContent-Type: {value.content_type}\r\n\r\n".encode("utf-8")
                yield from value.iter_chunks()
                yield b"\r\n"
            else:
                if isinstance(value, (dict, list)):
                    raise PlatformValidationError("Multipart fields must be scalar values or managed uploads.")
                text = "true" if value is True else "false" if value is False else "" if value is None else str(value)
                yield f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{text}\r\n".encode("utf-8")
        yield f"--{boundary}--\r\n".encode("ascii")

    def _validate_response(self, operation: OpenAPIOperation, status: int, value: Any) -> None:
        responses = operation.value.get("responses")
        response = responses.get(str(status)) if isinstance(responses, dict) else None
        response = response or (responses.get("default") if isinstance(responses, dict) else None)
        if response is None:
            return
        resolved = self._resolve(response)
        content = resolved.get("content") if isinstance(resolved, dict) else None
        media = content.get("application/json") if isinstance(content, dict) else None
        if isinstance(media, dict) and isinstance(media.get("schema"), dict):
            self._validate_schema(media["schema"], value, "response")

    def _auth(self, operation: OpenAPIOperation, requested: AuthType | Literal["auto"]) -> tuple[AuthType, str]:
        if requested != "auto":
            return requested, "X-Api-Key"
        security = operation.value.get("security", self.document.get("security", []))
        if not isinstance(security, list) or not security:
            return "none", "X-Api-Key"
        requirement = security[0]
        if not isinstance(requirement, dict) or len(requirement) != 1:
            raise PlatformValidationError("OpenAPI helper supports one security scheme per operation.")
        scheme_name = next(iter(requirement))
        schemes = self.document.get("components", {}).get("securitySchemes", {})
        scheme = schemes.get(scheme_name) if isinstance(schemes, dict) else None
        if not isinstance(scheme, dict):
            raise PlatformValidationError("OpenAPI security scheme is not defined.")
        if scheme.get("type") == "http" and scheme.get("scheme") == "bearer":
            return "bearer", "X-Api-Key"
        if scheme.get("type") == "http" and scheme.get("scheme") == "basic":
            return "basic", "X-Api-Key"
        if scheme.get("type") == "apiKey" and scheme.get("in") == "header" and isinstance(scheme.get("name"), str):
            return "api_key_header", str(scheme["name"])
        raise PlatformValidationError("OpenAPI security scheme is outside the supported subset.")

    def _validate_schema(self, schema: Any, value: Any, label: str) -> None:
        resolved = self._resolve(schema)
        if not isinstance(resolved, dict):
            raise PlatformValidationError(f"OpenAPI schema for {label} is invalid.")
        from app.services.integration_media_transfer import ManagedMediaUpload

        if isinstance(value, ManagedMediaUpload) and resolved.get("format") in {"binary", "byte", "lynewright-upload"}:
            return
        kind = resolved.get("type")
        if kind == "object":
            if not isinstance(value, dict):
                raise PlatformValidationError(f"OpenAPI {label} must be an object.")
            required = resolved.get("required") or []
            properties = resolved.get("properties") or {}
            for key in required:
                if key not in value:
                    raise PlatformValidationError(f"OpenAPI {label} requires {key}.")
            if isinstance(properties, dict):
                for key, nested in properties.items():
                    if key in value:
                        self._validate_schema(nested, value[key], f"{label}.{key}")
        elif kind == "array":
            if not isinstance(value, list):
                raise PlatformValidationError(f"OpenAPI {label} must be an array.")
            if isinstance(resolved.get("items"), dict):
                for index, item in enumerate(value):
                    self._validate_schema(resolved["items"], item, f"{label}[{index}]")
        elif kind == "string" and not isinstance(value, str):
            raise PlatformValidationError(f"OpenAPI {label} must be a string.")
        elif kind == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
            raise PlatformValidationError(f"OpenAPI {label} must be an integer.")
        elif kind == "number" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
            raise PlatformValidationError(f"OpenAPI {label} must be a number.")
        elif kind == "boolean" and not isinstance(value, bool):
            raise PlatformValidationError(f"OpenAPI {label} must be a boolean.")
        if "enum" in resolved and value not in resolved["enum"]:
            raise PlatformValidationError(f"OpenAPI {label} is not an allowed value.")

    def _resolve(self, value: Any) -> Any:
        if not isinstance(value, dict) or "$ref" not in value:
            return value
        reference = value["$ref"]
        if not isinstance(reference, str) or not reference.startswith("#/"):
            raise PlatformValidationError("OpenAPI helper supports local component references only.")
        current: Any = self.document
        for part in reference[2:].split("/"):
            if not isinstance(current, dict) or part not in current:
                raise PlatformValidationError("OpenAPI reference cannot be resolved.")
            current = current[part]
        return current

    @staticmethod
    def _document(value: dict[str, Any] | str | bytes) -> dict[str, Any]:
        if isinstance(value, bytes):
            if len(value) > MAX_DOCUMENT_BYTES:
                raise PlatformValidationError("OpenAPI document exceeds the 1 MB size limit.")
            value = value.decode("utf-8")
        if isinstance(value, str):
            if len(value.encode("utf-8")) > MAX_DOCUMENT_BYTES:
                raise PlatformValidationError("OpenAPI document exceeds the 1 MB size limit.")
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                try:
                    parsed = yaml.safe_load(value)
                except yaml.YAMLError as exc:
                    raise PlatformValidationError("OpenAPI document is not valid JSON or YAML.") from exc
        else:
            try:
                serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            except (TypeError, ValueError, RecursionError) as exc:
                raise PlatformValidationError("OpenAPI document is not a JSON-compatible object.") from exc
            if len(serialized) > MAX_DOCUMENT_BYTES:
                raise PlatformValidationError("OpenAPI document exceeds the 1 MB size limit.")
            parsed = value
        if not isinstance(parsed, dict) or not str(parsed.get("openapi") or "").startswith("3."):
            raise PlatformValidationError("OpenAPI helper supports OpenAPI 3.x documents only.")
        return parsed

    @staticmethod
    def _section(arguments: dict[str, Any], key: str) -> dict[str, Any]:
        value = arguments.get(key, {})
        if not isinstance(value, dict):
            raise PlatformValidationError(f"OpenAPI {key} arguments must be an object.")
        return value

    @staticmethod
    def _parameter_list(value: Any) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise PlatformValidationError("OpenAPI parameters must be a list.")
        return [item for item in value if isinstance(item, dict)]

    @staticmethod
    def _scalar(value: Any, label: str) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (str, int, float)):
            return str(value)
        raise PlatformValidationError(f"OpenAPI {label} must be scalar.")
