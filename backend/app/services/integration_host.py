"""The process-wide integration composition, owned by the service layer.

These singletons used to live in `routers/integration_gateway.py`, which meant
anything needing the live contribution registry had to import a router. A
service reaching into the HTTP layer inverts the dependency: routers are one
way to enter the system, not the place the system is assembled.

The registry has to be shared rather than rebuilt, because contributions are
registered into it when an integration activates. A second instance would be
correct in shape and empty in content -- which fails as "the provider does not
implement this", the least obvious way for wiring to be wrong.
"""
from __future__ import annotations

import os

from app.services.integration_connections import ConnectionProfileService
from app.services.integration_contributions import ContributionRegistry
from app.services.integration_gateway import IntegrationGateway
from app.services.integration_lifecycle import IntegrationLifecycleRuntime
from app.services.integration_registry import IntegrationRegistry
from app.services.platform_services import PlatformStore
from app.services.integration_worker_invocations import (
    DatabaseContainerInvocationBridge,
    DatabaseWorkerInvocationBridge,
)


store = PlatformStore()
integrations = IntegrationRegistry(store)
connections = ConnectionProfileService(store, registry=integrations)
contributions = ContributionRegistry(integrations, connections=connections)
contributions.container_bridge = DatabaseContainerInvocationBridge(store)
# API and MCP containers are unprivileged with respect to the renderer.  They
# opt into the durable worker bridge explicitly; the worker composition sets
# ``LYNEWRIGHT_RUNTIME_EXECUTION_PLANE=worker`` and keeps direct local execution.
if os.getenv("LYNEWRIGHT_RUNTIME_EXECUTION_PLANE", "").strip().lower() == "api":
    contributions.worker_bridge = DatabaseWorkerInvocationBridge(store)
runtime = IntegrationLifecycleRuntime(integrations, contributions)
gateway = IntegrationGateway(store, connections, contributions)


__all__ = [
    "connections", "contributions", "gateway", "integrations", "runtime", "store",
]
