"""Public generic Integration identity and authorization API."""
from app.services.extension_capabilities import (
    ExtensionActor as IntegrationActor,
    ExtensionAuthorizationService as IntegrationAuthorizationService,
    PlatformPolicy,
    ProjectScopePolicy,
)

__all__ = [
    "IntegrationActor",
    "IntegrationAuthorizationService",
    "PlatformPolicy",
    "ProjectScopePolicy",
]
