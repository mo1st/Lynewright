"""Core MCP transport with registry-provided tools and optional plugins."""
from __future__ import annotations

import importlib
import inspect
import os
from contextlib import asynccontextmanager
from typing import Any, Callable, Literal
from urllib.parse import urlsplit

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl

from app.config import get_settings
from app.services.core_tools import CoreToolRuntime
from app.services.integration_contributions import ContributionRegistry
from app.services.integration_operations import register_generic_integration_tools
from app.services.assistant_tools import register_assistant_tools
from app.services.mcp_oauth import (
    MCPClientRevocationHandler, MCPOAuthRequestGuard, MCPOAuthService,
    MCPAuthorizationProvider, canonical_public_origin, mcp_oauth_enabled,
)
from app.services.platform_capabilities import CapabilityActor, PUBLIC_CAPABILITIES, allowed_capabilities
from app.services.platform_services import PlatformServiceError, PlatformStore
from app.services.tenant_schema import DEFAULT_TENANT_ID
from app.services.tool_models import MAX_TOOL_INPUT_BYTES


class MCPToolRegistry(CoreToolRuntime):
    """Tool registry bound to the authenticated MCP request actor."""

    def __init__(
        self, store: PlatformStore | None = None, *, max_tool_calls_per_minute: int = 60,
    ) -> None:
        super().__init__(store=store)
        self.oauth_service: MCPOAuthService | None = None
        self.max_tool_calls_per_minute = max(1, int(max_tool_calls_per_minute))

    def current_actor(self) -> CapabilityActor:
        access_token = get_access_token()
        if access_token is None:
            raise ValueError("Authentication required.")
        if access_token.token.startswith("qxc_"):
            return self.clients.authenticate(access_token.token)
        if self.oauth_service is None:
            raise ValueError("OAuth authorization is unavailable.")
        return self.oauth_service.actor_for_access_token(access_token)

    async def invoke_current(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        actor = self.current_actor()
        from app.services.rate_limiter import RateLimiterUnavailable, get_rate_limiter

        try:
            allowed = get_rate_limiter().consume(
                namespace="mcp_tool_call", key=actor.client_id,
                maximum=self.max_tool_calls_per_minute, window_seconds=60,
            )
        except RateLimiterUnavailable as exc:
            raise PlatformServiceError("MCP tool calls are temporarily unavailable.") from exc
        if not allowed:
            raise PlatformServiceError("MCP tool call rate limit exceeded.")
        return self.invoke(name, actor, arguments or {})


def _tool_visible_to_actor(tool: Any, actor: CapabilityActor) -> bool:
    """Hide tools whose declared capability was not granted to this client."""
    metadata = getattr(tool, "meta", None)
    capability = metadata.get("capability") if isinstance(metadata, dict) else None
    if not isinstance(capability, str):
        # Plugins without capability metadata are only exposed to full-access
        # clients; restricted clients fail closed on unclassified tools.
        return actor.internal or actor.permission_mode == "full"
    return capability in allowed_capabilities(actor)


_ASSISTANT_TOOL_NAMES = frozenset({
    "lines.list", "lines.get", "runs.list", "runs.get", "runs.start",
    "runs.cancel", "runs.retry_stage", "reviews.list", "reviews.get", "reviews.decide",
})


def _assistant_tool_callable(registry: MCPToolRegistry, name: str) -> Callable[..., Any]:
    """Expose assistant tool properties as ordinary MCP arguments, not a nested bag."""
    definition = registry.get_definition(name)
    schema = definition.input_schema if definition is not None else None
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    required = set(schema.get("required", [])) if isinstance(schema, dict) else set()
    parameters: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {}
    for field_name, field_schema in properties.items():
        if not isinstance(field_schema, dict):
            continue
        kind = field_schema.get("type")
        python_type: Any = {
            "string": str, "integer": int, "number": float,
            "boolean": bool, "object": dict[str, Any], "array": list[Any],
        }.get(kind, Any)
        enum = field_schema.get("enum")
        if isinstance(enum, list) and enum:
            python_type = Literal[tuple(enum)]
        default = inspect.Parameter.empty if field_name in required else ({} if kind == "object" else None)
        parameters.append(inspect.Parameter(field_name, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=python_type))
        annotations[field_name] = python_type

    async def invoke(**arguments: Any) -> Any:
        return await registry.invoke_current(name, arguments)

    invoke.__name__ = name.replace(".", "_")
    invoke.__doc__ = definition.description if definition is not None else ""
    invoke.__signature__ = inspect.Signature(parameters, return_annotation=Any)
    invoke.__annotations__ = {**annotations, "return": Any}
    return invoke


class LynewrightTokenVerifier:
    """Adapter for provisioned MCP client tokens."""
    def __init__(self, clients: Any) -> None:
        self.clients = clients

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            actor = self.clients.authenticate(token)
        except PlatformServiceError:
            return None
        return AccessToken(
            token=token, client_id=actor.client_id,
            scopes=sorted(allowed_capabilities(actor)),
            subject=actor.client_id,
        )


def load_plugins(app: Any) -> list[str]:
    """Call register(mcp) on modules named by LYNEWRIGHT_MCP_PLUGINS."""
    specs = [item.strip() for item in os.getenv("LYNEWRIGHT_MCP_PLUGINS", "").split(",") if item.strip()]
    loaded: list[str] = []
    for module_name in specs:
        module = importlib.import_module(module_name)
        register = getattr(module, "register", None)
        if not callable(register):
            raise TypeError(f"Lynewright plugin {module_name!r} has no register(app) function")
        register(app)
        loaded.append(module_name)
    return loaded


def create_mcp_server(
    store: PlatformStore | None = None, *,
    contributions: ContributionRegistry | None = None,
    max_tool_calls_per_minute: int = 60,
) -> tuple[FastMCP, MCPToolRegistry]:
    settings = get_settings()
    base_url = canonical_public_origin(settings.public_url or "http://127.0.0.1:8000")
    resource_url = f"{base_url}/mcp"
    hostname = (urlsplit(base_url).hostname or "127.0.0.1").lower()
    registry = MCPToolRegistry(store, max_tool_calls_per_minute=max_tool_calls_per_minute)
    oauth = MCPOAuthService(store, issuer_url=f"{base_url}/", resource_url=resource_url)
    registry.oauth_service = oauth
    register_assistant_tools(registry)
    provider = MCPAuthorizationProvider(oauth, registry.clients)
    oauth_enabled = mcp_oauth_enabled()
    if not oauth_enabled:
        oauth.revoke_all_grants()
    mcp = FastMCP(
        "Lynewright", instructions="Run declared integration operations through registered tools.",
        auth=AuthSettings(
            issuer_url=base_url, resource_server_url=resource_url,
            client_registration_options=ClientRegistrationOptions(
                enabled=oauth_enabled, valid_scopes=sorted(PUBLIC_CAPABILITIES), default_scopes=None,
            ),
            revocation_options=RevocationOptions(enabled=oauth_enabled),
        ),
        streamable_http_path="/mcp", stateless_http=True, json_response=True,
        max_request_body_size=MAX_TOOL_INPUT_BYTES,
        transport_security=TransportSecuritySettings(
            allowed_hosts=[hostname, f"{hostname}:*", "127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*", "mcp", "mcp:*"],
            allowed_origins=[base_url],
        ),
        **({"auth_server_provider": provider} if oauth_enabled else {
            "token_verifier": LynewrightTokenVerifier(registry.clients)
        }),
    )
    # Only the standalone service uses the shared persistent host. Tests and
    # embedded callers may supply an isolated store with their own providers.
    mcp._lynewright_activate_integrations = store is None
    if contributions is not None:
        register_generic_integration_tools(
            registry, tenant_id=DEFAULT_TENANT_ID, contributions=contributions,
        )
    for definition in registry.definitions():
        if definition.name in _ASSISTANT_TOOL_NAMES:
            mcp.add_tool(
                _assistant_tool_callable(registry, definition.name),
                name=definition.name, description=definition.description,
                annotations=ToolAnnotations(
                    readOnlyHint=definition.read_only, destructiveHint=definition.destructive,
                ),
                meta={"input_schema": definition.input_schema, "capability": definition.capability},
            )
            continue
        async def _tool(arguments: dict[str, Any], *, name: str = definition.name) -> Any:
            return await registry.invoke_current(name, arguments)
        mcp.tool(
            name=definition.name, description=definition.description,
            annotations=ToolAnnotations(
                readOnlyHint=definition.read_only, destructiveHint=definition.destructive,
            ),
            meta={"input_schema": definition.input_schema, "capability": definition.capability},
        )(_tool)
    load_plugins(mcp)

    # FastMCP's default handler lists every registered tool. Replace it with
    # the same authenticated actor resolution used by calls so restricted MCP
    # clients never see actions that their grants would reject.
    @mcp._mcp_server.list_tools()
    async def list_tools_for_current_actor() -> list[Any]:
        actor = registry.current_actor()
        return [tool for tool in await mcp.list_tools() if _tool_visible_to_actor(tool, actor)]

    return mcp, registry


def create_mcp_http_app(mcp: FastMCP, *, oauth_provider: MCPAuthorizationProvider) -> MCPOAuthRequestGuard:
    origin = canonical_public_origin(get_settings().public_url or "http://127.0.0.1:8000")
    transport_app = mcp.streamable_http_app()
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    original_lifespan = transport_app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: Any):
        async with original_lifespan(application):
            # Contributions are process-local. The API's startup cannot
            # populate the standalone MCP service's operation registry, so
            # use the shared secondary-process activation path before health
            # reports ready. It never recovers API-only definitions.
            if getattr(mcp, "_lynewright_activate_integrations", False):
                from app.services.integration_activation import activate_integrations

                activate_integrations(source="lynewright.mcp.startup", recover_enabled=False)
            yield

    transport_app.router.lifespan_context = lifespan

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    transport_app.router.routes.insert(0, Route("/health", health, methods=["GET"]))
    if mcp_oauth_enabled():
        from starlette.routing import Route

        revocation_handler = MCPClientRevocationHandler(oauth_provider)
        transport_app.router.routes = [
            Route("/revoke", revocation_handler.handle, methods=["POST", "OPTIONS"])
            if getattr(route, "path", None) == "/revoke" else route
            for route in transport_app.router.routes
        ]
    return MCPOAuthRequestGuard(
        transport_app, issuer_url=f"{origin}/",
        resource_url=f"{origin}/mcp", oauth_enabled=mcp_oauth_enabled(),
    )


mcp, registry = create_mcp_server()
app = create_mcp_http_app(mcp, oauth_provider=MCPAuthorizationProvider(
    registry.oauth_service, registry.clients,
))
