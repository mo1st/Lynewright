"""Public generic Integration Registry API.

`ExtensionRegistry` is retained as a private compatibility implementation from
the first three accepted milestones. New infrastructure must import this
provider-neutral name instead.
"""
from app.services.extension_registry import ExtensionRegistry

IntegrationRegistry = ExtensionRegistry

__all__ = ["IntegrationRegistry"]
