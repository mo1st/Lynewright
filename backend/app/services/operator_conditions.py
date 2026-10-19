"""Deterministic, bounded condition evaluation for operator automations.

Session E owns triggers. Conditions decide *whether an objective should be
requested* — that decision must never need a model, so this evaluator is pure:
the same metrics in, the same answer out.

Two hard boundaries:

* Inputs come from registered metric resolvers only — never arbitrary SQL,
  Python expressions, or model-generated code. The grammar is a flat list of
  clauses over named metrics with a fixed operator set:

      eq neq gt gte lt lte in changed percent_change_gt

  All clauses must hold ("all" semantics). A clause whose metric cannot be
  resolved evaluates to ``None`` (not evaluated), and an unevaluated clause
  never triggers — guessing either direction would turn missing data into
  false alarms or silent blindness.
* State for ``changed`` / ``percent_change_gt`` lives outside the evaluator.
  Callers persist baselines between evaluations; the evaluator itself is
  stateless and therefore safe to run from any process.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app.services.platform_services import PlatformValidationError


CONDITION_OPERATORS: tuple[str, ...] = (
    "eq", "neq", "gt", "gte", "lt", "lte", "in", "changed", "percent_change_gt",
)
MAX_CLAUSES = 8
MAX_METRIC_NAME_LENGTH = 96

MetricResolver = Callable[[dict[str, Any]], Any]


@dataclass(frozen=True)
class ClauseResult:
    index: int
    metric: str
    operator: str
    observed: Any
    satisfied: bool | None  # None = not evaluated (missing metric/baseline)

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "metric": self.metric,
            "operator": self.operator,
            "observed": self.observed,
            "satisfied": self.satisfied,
        }


def validate_condition_spec(spec: Any) -> dict[str, Any]:
    """Validate a condition trigger config without resolving any metric."""
    source = spec if isinstance(spec, dict) else {}
    clauses = source.get("all")
    if not isinstance(clauses, list) or not clauses:
        raise PlatformValidationError("Condition automations require a non-empty 'all' clause list.")
    if len(clauses) > MAX_CLAUSES:
        raise PlatformValidationError(f"Condition automations accept at most {MAX_CLAUSES} clauses.")
    normalized: list[dict[str, Any]] = []
    for raw in clauses:
        clause = raw if isinstance(raw, dict) else {}
        name = str(clause.get("metric") or "").strip()
        if not name or len(name) > MAX_METRIC_NAME_LENGTH:
            raise PlatformValidationError("Each condition clause needs a bounded metric name.")
        operator = str(clause.get("op") or "").strip().lower()
        if operator not in CONDITION_OPERATORS:
            raise PlatformValidationError(
                f"Unknown condition operator {operator!r}; allowed: {', '.join(CONDITION_OPERATORS)}."
            )
        entry: dict[str, Any] = {"metric": name, "op": operator}
        if "value" in clause:
            entry["value"] = clause["value"]
        args = clause.get("metric_args")
        if args is not None:
            if not isinstance(args, dict):
                raise PlatformValidationError("metric_args must be an object.")
            entry["metric_args"] = dict(args)
        window = clause.get("window_seconds")
        if window is not None:
            try:
                window_value = int(window)
            except (TypeError, ValueError) as exc:
                raise PlatformValidationError("window_seconds must be an integer.") from exc
            if not 0 < window_value <= 30 * 24 * 3600:
                raise PlatformValidationError("window_seconds must be between 1 and 30 days.")
            entry["window_seconds"] = window_value
        normalized.append(entry)
    return {"all": normalized}


def resolve_metrics(
    spec: dict[str, Any],
    resolvers: dict[str, MetricResolver],
    scope: dict[str, Any],
) -> dict[str, tuple[Any, str]]:
    """Resolve every referenced metric once per evaluation.

    Returns ``{clause_key: (value, resolver_name)}`` where ``clause_key`` is the
    stable ``name`` of the metric (clauses sharing a metric share one lookup).
    Unresolvable metrics yield ``None`` values rather than exceptions: one bad
    resolver must not abort the whole evaluation.
    """
    resolved: dict[str, tuple[Any, str]] = {}
    for clause in spec.get("all", []):
        name = str(clause["metric"])
        if name in resolved:
            continue
        resolver = resolvers.get(name)
        if resolver is None:
            resolved[name] = (None, "")
            continue
        call_scope = dict(scope)
        call_scope.update(clause.get("metric_args") or {})
        if "window_seconds" in clause:
            call_scope.setdefault("window_seconds", clause["window_seconds"])
        try:
            resolved[name] = (resolver(call_scope), name)
        except Exception:
            resolved[name] = (None, name)
    return resolved


def evaluate_conditions(
    spec: Any,
    resolved: dict[str, Any],
    *,
    baselines: dict[str, float | None] | None = None,
) -> dict[str, Any]:
    """Evaluate a validated condition spec against already-resolved metrics.

    ``resolved`` maps metric name -> observed value. ``baselines`` maps metric
    name -> previously persisted value for ``changed`` /
    ``percent_change_gt`` clauses.
    """
    previous = baselines if isinstance(baselines, dict) else {}
    results: list[ClauseResult] = []
    next_baselines: dict[str, float | None] = {}
    for index, clause in enumerate(spec.get("all", [])):
        name = str(clause["metric"])
        operator = str(clause["op"])
        observed = resolved.get(name)
        if operator == "changed":
            satisfied = _changed(observed, previous.get(name))
        elif operator == "percent_change_gt":
            threshold = _number(clause.get("value"))
            satisfied = _percent_change(observed, previous.get(name), threshold)
        else:
            satisfied = _compare(observed, operator, clause.get("value"))
        results.append(ClauseResult(index=index, metric=name, operator=operator, observed=observed, satisfied=satisfied))
        # Baselines advance for every numeric observation, triggered or not,
        # so `changed` measures movement since the last evaluation tick.
        number = _number(observed)
        next_baselines[name] = number
    triggered = bool(results) and all(item.satisfied is True for item in results)
    return {
        "triggered": triggered,
        "results": [item.as_dict() for item in results],
        "not_evaluated": [item.metric for item in results if item.satisfied is None],
        "next_baselines": next_baselines,
    }


def register_default_resolvers(registry: dict[str, MetricResolver], store: Any, *, tenant_id: str) -> dict[str, MetricResolver]:
    """Attach the built-in platform metric resolvers to a registry dict.

    Resolvers read only small aggregate queries over existing platform tables;
    they never expose rows to conditions. Unknown/failed lookups become None.
    """
    def jobs_count(scope: dict[str, Any], statuses: tuple[str, ...]) -> int | None:
        try:
            with store.connection() as db:
                window = max(0, min(int(scope.get("window_seconds") or 24 * 3600), 30 * 24 * 3600))
                placeholders = ",".join("?" for _ in statuses)
                row = db.execute(
                    f"""SELECT COUNT(*) AS total FROM platform_jobs
                    WHERE tenant_id=? AND status IN ({placeholders})
                    AND created_at >= datetime('now', ?)""",
                    (tenant_id, *statuses, f"-{int(window)} seconds"),
                ).fetchone()
            return int(row["total"]) if row is not None else 0
        except Exception:
            return None

    def failed_jobs(scope: dict[str, Any]) -> int | None:
        return jobs_count(scope, ("failed",))

    def pending_jobs(scope: dict[str, Any]) -> int | None:
        return jobs_count(scope, ("queued", "running"))

    def event_count(scope: dict[str, Any]) -> int | None:
        event_type = str(scope.get("event_type") or "").strip()
        if not event_type:
            return None
        try:
            with store.connection() as db:
                window = max(0, min(int(scope.get("window_seconds") or 24 * 3600), 30 * 24 * 3600))
                row = db.execute(
                    """SELECT COUNT(*) AS total FROM platform_events
                    WHERE tenant_id=? AND event_type=?
                    AND created_at >= datetime('now', ?)""",
                    (tenant_id, event_type, f"-{int(window)} seconds"),
                ).fetchone()
            return int(row["total"]) if row is not None else 0
        except Exception:
            return None

    registry.update({
        "jobs.failed_count": failed_jobs,
        "jobs.pending_count": pending_jobs,
        "events.count": event_count,
    })
    return registry


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _compare(observed: Any, operator: str, expected: Any) -> bool | None:
    if operator == "eq":
        return None if observed is None else observed == expected
    if operator == "neq":
        return None if observed is None else observed != expected
    if operator == "in":
        members = expected if isinstance(expected, list) else None
        if members is None:
            return False
        return None if observed is None else any(observed == member for member in members)
    left, right = _number(observed), _number(expected)
    if right is None:
        return False
    if left is None:
        return None
    if operator == "gt":
        return left > right
    if operator == "gte":
        return left >= right
    if operator == "lt":
        return left < right
    if operator == "lte":
        return left <= right
    return None


def _changed(observed: Any, baseline: Any) -> bool | None:
    current = _number(observed)
    if current is None:
        return None
    if baseline is None:
        # No prior observation: report movement honestly instead of inventing
        # a first value that would make every first tick a trigger.
        return False
    return current != float(baseline)


def _percent_change(observed: Any, baseline: Any, threshold: Any) -> bool | None:
    current = _number(observed)
    previous = _number(baseline)
    limit = _number(threshold)
    if current is None or limit is None:
        return None
    if previous is None or previous == 0:
        return None
    change_percent = abs(current - previous) / abs(previous) * 100.0
    return change_percent > limit


__all__ = [
    "CONDITION_OPERATORS",
    "ClauseResult",
    "MAX_CLAUSES",
    "evaluate_conditions",
    "register_default_resolvers",
    "resolve_metrics",
    "validate_condition_spec",
]
