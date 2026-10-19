"""Optional bridge to an external source-change provider."""
from __future__ import annotations
from typing import Any, Protocol, Callable

class BuilderAgentHook(Protocol):
    def status(self) -> dict[str, Any]: ...
    def generate_change_set(self, *, project_id: str, user_request: str) -> dict[str, Any]: ...
    def get_change_set(self, change_set_id: int) -> dict[str, Any] | None: ...
    def apply_change_set(self, change_set_id: int) -> dict[str, Any]: ...
    def verify_change_set(self, change_set_id: int) -> dict[str, Any]: ...
    def rollback_change_set(self, change_set_id: int) -> dict[str, Any]: ...

_hook: BuilderAgentHook | None = None

def register_builder_agent_hook(hook: BuilderAgentHook) -> Callable[[], bool]:
    """Register an optional provider; Core remains usable without one."""
    global _hook
    previous = _hook
    _hook = hook
    def unregister() -> bool:
        global _hook
        if _hook is not hook:
            return False
        _hook = previous
        return True
    return unregister

def get_builder_agent_hook() -> BuilderAgentHook | None:
    return _hook
