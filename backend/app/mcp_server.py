"""Core MCP transport with registry-provided tools and optional plugins."""
from __future__ import annotations

import importlib
import os
from typing import Any, Callable
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
from app.services.mcp_oauth import (
    MCPClientRevocationHandler, MCPOAuthRequestGuard, MCPOAuthService,
    MCPAuthorizationProvider, canonical_public_origin, mcp_oauth_enabled,
)
from app.services.platform_capabilities import PUBLIC_CAPABILITIES, allowed_capabilities
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

    async def invoke_current(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        access_token = get_access_token()
        if access_token is None:
            raise ValueError("Authentication required.")
        if access_token.token.startswith("qxc_"):
            actor = self.clients.authenticate(access_token.token)
        else:
            if self.oauth_service is None:
                raise ValueError("OAuth authorization is unavailable.")
            actor = self.oauth_service.actor_for_access_token(access_token)
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
    """Call register(app) on every module named by LYNEWRIGHT_PLUGINS."""
    specs = [item.strip() for item in os.getenv("LYNEWRIGHT_PLUGINS", "").split(",") if item.strip()]
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
            allowed_hosts=[hostname, f"{hostname}:*"], allowed_origins=[base_url],
        ),
        **({"auth_server_provider": provider} if oauth_enabled else {
            "token_verifier": LynewrightTokenVerifier(registry.clients)
        }),
    )
    if contributions is not None:
        register_generic_integration_tools(
            registry, tenant_id=DEFAULT_TENANT_ID, contributions=contributions,
        )
    for definition in registry.definitions():
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
    return mcp, registry


def create_mcp_http_app(mcp: FastMCP, *, oauth_provider: MCPAuthorizationProvider) -> MCPOAuthRequestGuard:
    origin = canonical_public_origin(get_settings().public_url or "http://127.0.0.1:8000")
    transport_app = mcp.streamable_http_app()
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
