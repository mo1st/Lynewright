"""Deterministic activation dependency graph with cycle diagnostics."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.services.platform_services import PlatformConflictError, PlatformValidationError


ReadinessState = Literal["REGISTERED", "WAITING", "READY", "SUSPENDED", "FAILED"]


@dataclass(frozen=True, slots=True)
class Dependency:
    kind: Literal["integration", "contract", "feature_flag", "credential", "connection", "host_schema"]
    id: str
    version: str = ""

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.id}@{self.version}" if self.version else f"{self.kind}:{self.id}"


@dataclass(slots=True)
class DependencyNode:
    node_id: str
    requires: tuple[Dependency, ...]
    available: set[str] = field(default_factory=set)
    state: ReadinessState = "REGISTERED"

    def evaluate(self) -> ReadinessState:
        self.state = "READY" if all(item.key in self.available for item in self.requires) else "WAITING"
        return self.state


class DependencyReadinessGraph:
    """Small in-process graph; resource readiness is intentionally out of scope."""

    def __init__(self) -> None:
        self._nodes: dict[str, DependencyNode] = {}

    def register(self, node_id: str, requires: list[Dependency] | tuple[Dependency, ...] = ()) -> DependencyNode:
        if not node_id or node_id in self._nodes:
            raise PlatformConflictError("Dependency graph node is already registered.")
        normalized = tuple(sorted(requires, key=lambda item: item.key))
        if len({item.key for item in normalized}) != len(normalized):
            raise PlatformValidationError("Dependency declarations must be unique.")
        self._nodes[node_id] = DependencyNode(node_id=node_id, requires=normalized)
        try:
            self._assert_acyclic()
        except Exception:
            # A rejected declaration must not leave a half-registered node that
            # changes later diagnostics or makes a retry non-deterministic.
            del self._nodes[node_id]
            raise
        self._nodes[node_id].evaluate()
        return self._nodes[node_id]

    def set_available(self, dependency: Dependency, *, available: bool = True) -> None:
        key = dependency.key
        for node in self._nodes.values():
            if available:
                node.available.add(key)
            else:
                node.available.discard(key)
            node.evaluate()

    def node(self, node_id: str) -> DependencyNode:
        try:
            return self._nodes[node_id]
        except KeyError as exc:
            raise PlatformValidationError("Dependency graph node was not found.") from exc

    def nodes(self) -> tuple[DependencyNode, ...]:
        """Return a deterministic snapshot for lifecycle coordination."""
        return tuple(self._nodes[node_id] for node_id in sorted(self._nodes))

    def diagnostics(self, node_id: str) -> dict[str, object]:
        node = self.node(node_id)
        ready = [item.key for item in node.requires if item.key in node.available]
        waiting = [item.key for item in node.requires if item.key not in node.available]
        return {"node_id": node_id, "state": node.state, "ready_dependencies": ready, "waiting_on": waiting}

    def _assert_acyclic(self) -> None:
        # Integration dependencies can reference another registered node. Other
        # dependency kinds are external leaves.
        edges = {
            node_id: [item.id for item in node.requires if item.kind == "integration" and item.id in self._nodes]
            for node_id, node in self._nodes.items()
        }
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str, path: list[str]) -> None:
            if node_id in visiting:
                cycle = " -> ".join(path + [node_id])
                raise PlatformValidationError(f"DEPENDENCY_CYCLE: {cycle}")
            if node_id in visited:
                return
            visiting.add(node_id)
            for child in edges[node_id]:
                visit(child, path + [node_id])
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in sorted(edges):
            visit(node_id, [])


__all__ = ["Dependency", "DependencyNode", "DependencyReadinessGraph", "ReadinessState"]
