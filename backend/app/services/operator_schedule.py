"""Bounded calendar scheduling for operator automations.

Session E owns triggers and schedules. This module is the single description of
what an automation schedule *means*, so a stored ``trigger_config_json`` always
computes the same due times regardless of which process evaluates it.

The format is deliberately narrow — three kinds, no cron expressions:

    {"kind": "daily", "time": "12:00", "timezone": "Europe/Berlin"}
    {"kind": "weekly", "days": ["mon", "fri"], "time": "09:30", "timezone": "UTC"}
    {"kind": "interval", "interval_seconds": 3600}

Business time zones are explicit: server UTC must never silently stand in for
the merchant's day, so a daily/weekly schedule without a resolvable timezone is
rejected rather than guessed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.services.platform_services import PlatformValidationError


SCHEDULE_KINDS: tuple[str, ...] = ("daily", "weekly", "interval")
WEEKDAY_NAMES: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MIN_INTERVAL_SECONDS = 60
MAX_INTERVAL_SECONDS = 7 * 24 * 3600
_MAX_TIMEZONE_LENGTH = 64

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


@dataclass(frozen=True)
class NormalizedSchedule:
    """A validated schedule; everything needed to compute the next due time."""

    kind: str
    timezone: str  # "" for interval schedules (they are timezone-independent)
    time: str  # "HH:MM" local to `timezone`; "" for interval schedules
    days: tuple[str, ...]  # weekday names; empty except weekly
    interval_seconds: int  # 0 except interval schedules


def parse_schedule(config: Any) -> NormalizedSchedule:
    """Validate one automation trigger config into a NormalizedSchedule."""
    source = config if isinstance(config, dict) else {}
    kind = str(source.get("kind") or "").strip().lower()
    if kind not in SCHEDULE_KINDS:
        raise PlatformValidationError(
            "Schedule kind must be one of: daily, weekly, interval."
        )
    if kind == "interval":
        return _parse_interval(source)
    zone_name = str(source.get("timezone") or "").strip()
    if not zone_name or len(zone_name) > _MAX_TIMEZONE_LENGTH:
        raise PlatformValidationError("Scheduled automations require an explicit IANA timezone.")
    try:
        ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise PlatformValidationError(f"Unknown timezone: {zone_name}") from exc
    clock = str(source.get("time") or "").strip()
    if not _TIME_RE.fullmatch(clock):
        raise PlatformValidationError("Schedule time must use 24-hour HH:MM.")
    if kind == "daily":
        return NormalizedSchedule(kind="daily", timezone=zone_name, time=clock, days=(), interval_seconds=0)
    return _parse_weekly(source, zone_name=zone_name, clock=clock)


def next_due_at(schedule: NormalizedSchedule, after_epoch: float, *, now_epoch: float | None = None) -> float:
    """The first epoch strictly after ``after_epoch`` when the schedule is due.

    Interval schedules anchor on ``after_epoch`` itself. Calendar schedules
    search forward day by day in the schedule's own timezone, so a UTC process
    and a local process agree on what "12:00 in Berlin" means.
    """
    if not isinstance(after_epoch, (int, float)) or after_epoch != after_epoch:  # NaN guard
        raise PlatformValidationError("Schedule cursor must be a finite epoch timestamp.")
    if schedule.kind == "interval":
        interval = max(MIN_INTERVAL_SECONDS, schedule.interval_seconds)
        return float(after_epoch) + interval
    reference = datetime.fromtimestamp(float(after_epoch), tz=dt_timezone.utc).astimezone(ZoneInfo(schedule.timezone))
    hours, minutes = (int(part) for part in schedule.time.split(":"))
    # Daily schedules are due every day; weekly only on the configured names.
    allowed_days = set(WEEKDAY_NAMES) if schedule.kind == "daily" else set(schedule.days)
    for day_offset in range(0, 366):
        candidate = (reference + timedelta(days=day_offset)).replace(hour=hours, minute=minutes, second=0, microsecond=0)
        if WEEKDAY_NAMES[candidate.weekday()] not in allowed_days:
            continue
        candidate_epoch = candidate.timestamp()
        if candidate_epoch > float(after_epoch):
            return candidate_epoch
    raise PlatformValidationError("Schedule has no due time within one year.")


def initial_due_at(schedule: NormalizedSchedule, now_epoch: float) -> float:
    """First due time for a freshly created automation.

    A new automation fires at its next natural slot rather than immediately;
    interval schedules are anchored on creation time so restarts cannot pull
    them forward.
    """
    if schedule.kind == "interval":
        return float(now_epoch)
    return next_due_at(schedule, now_epoch)


def advance_due_at(schedule: NormalizedSchedule, previous_due_epoch: float, now_epoch: float) -> float:
    """Next due time strictly after ``previous_due`` (used while catching up)."""
    return next_due_at(schedule, previous_due_epoch)


def describe_schedule(schedule: NormalizedSchedule) -> dict[str, Any]:
    return {
        "kind": schedule.kind,
        "timezone": schedule.timezone,
        "time": schedule.time,
        "days": list(schedule.days),
        "interval_seconds": schedule.interval_seconds,
    }


def _parse_interval(source: dict[str, Any]) -> NormalizedSchedule:
    raw = source.get("interval_seconds")
    try:
        interval = int(raw)  # rejects floats-with-fraction and non-numbers
    except (TypeError, ValueError) as exc:
        raise PlatformValidationError("Interval schedules require integer interval_seconds.") from exc
    if isinstance(raw, float) and not raw.is_integer():
        raise PlatformValidationError("Interval schedules require integer interval_seconds.")
    if not MIN_INTERVAL_SECONDS <= interval <= MAX_INTERVAL_SECONDS:
        raise PlatformValidationError(
            f"Interval seconds must be between {MIN_INTERVAL_SECONDS} and {MAX_INTERVAL_SECONDS}."
        )
    return NormalizedSchedule(kind="interval", timezone="", time="", days=(), interval_seconds=interval)


def _parse_weekly(source: dict[str, Any], *, zone_name: str, clock: str) -> NormalizedSchedule:
    days_raw = source.get("days")
    if not isinstance(days_raw, list) or not days_raw:
        raise PlatformValidationError("Weekly schedules require at least one weekday.")
    days: list[str] = []
    for item in days_raw[:7]:
        name = str(item or "").strip().lower()[:3]
        if name not in WEEKDAY_NAMES:
            raise PlatformValidationError(f"Unknown weekday: {item!r}")
        if name not in days:
            days.append(name)
    ordered = tuple(name for name in WEEKDAY_NAMES if name in days)
    return NormalizedSchedule(kind="weekly", timezone=zone_name, time=clock, days=ordered, interval_seconds=0)


__all__ = [
    "MAX_INTERVAL_SECONDS",
    "MIN_INTERVAL_SECONDS",
    "NormalizedSchedule",
    "SCHEDULE_KINDS",
    "WEEKDAY_NAMES",
    "advance_due_at",
    "describe_schedule",
    "initial_due_at",
    "next_due_at",
    "parse_schedule",
]
