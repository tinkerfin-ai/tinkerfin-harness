"""Validated public declarations for one-time and recurring schedules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Annotated, Literal, Self, TypeAlias
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from .errors import InvalidScheduleError
from .policies import MisfireMode, MisfirePolicy

MIN_INTERVAL_SECONDS = 60
MAX_INTERVAL_SECONDS = 365 * 24 * 60 * 60
MAX_CRON_SEARCH_YEARS = 8
_MAX_MISFIRE_SCAN = 100_000
_MONTH_NAMES = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_WEEKDAY_NAMES = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}


def _aware_utc(value: datetime, *, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _parse_value(
    value: str,
    *,
    minimum: int,
    maximum: int,
    names: dict[str, int] | None,
) -> int:
    normalized = value.lower()
    if names is not None and normalized in names:
        return names[normalized]
    try:
        parsed = int(normalized)
    except ValueError as error:
        raise InvalidScheduleError(
            "Cron field contains an unsupported value",
            context={"value": value},
            cause=error,
        ) from error
    if not minimum <= parsed <= maximum:
        raise InvalidScheduleError(
            "Cron field value is outside its allowed range",
            context={"value": value, "minimum": minimum, "maximum": maximum},
        )
    return parsed


def _parse_field(
    field: str,
    *,
    minimum: int,
    maximum: int,
    names: dict[str, int] | None = None,
) -> frozenset[int]:
    selected: set[int] = set()
    for item in field.split(","):
        if not item:
            raise InvalidScheduleError("Cron fields cannot contain empty list items")
        base, separator, step_text = item.partition("/")
        if separator:
            try:
                step = int(step_text)
            except ValueError as error:
                raise InvalidScheduleError(
                    "Cron step must be a positive integer",
                    context={"value": step_text},
                    cause=error,
                ) from error
            if step < 1:
                raise InvalidScheduleError("Cron step must be a positive integer")
        else:
            step = 1

        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            start_text, end_text = base.split("-", 1)
            start = _parse_value(
                start_text, minimum=minimum, maximum=maximum, names=names
            )
            end = _parse_value(end_text, minimum=minimum, maximum=maximum, names=names)
            if start > end:
                raise InvalidScheduleError(
                    "Cron ranges must be ordered from lower to higher"
                )
        else:
            start = _parse_value(base, minimum=minimum, maximum=maximum, names=names)
            end = start
        selected.update(range(start, end + 1, step))
    return frozenset(selected)


@dataclass(frozen=True, slots=True)
class _CompiledCron:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]


def _compile_cron(expression: str) -> _CompiledCron:
    fields = expression.split()
    if len(fields) != 5:
        raise InvalidScheduleError("Cron expression must contain exactly five fields")
    return _CompiledCron(
        minutes=_parse_field(fields[0], minimum=0, maximum=59),
        hours=_parse_field(fields[1], minimum=0, maximum=23),
        days=_parse_field(fields[2], minimum=1, maximum=31),
        months=_parse_field(fields[3], minimum=1, maximum=12, names=_MONTH_NAMES),
        weekdays=_parse_field(fields[4], minimum=0, maximum=6, names=_WEEKDAY_NAMES),
    )


def _valid_utc_candidates(
    local_value: datetime, zone: ZoneInfo
) -> tuple[datetime, ...]:
    candidates: set[datetime] = set()
    for fold in (0, 1):
        aware = local_value.replace(tzinfo=zone, fold=fold)
        candidate = aware.astimezone(UTC)
        if candidate.astimezone(zone).replace(tzinfo=None) == local_value:
            candidates.add(candidate)
    return tuple(sorted(candidates))


class _ScheduleModel(BaseModel):
    """Validate immutable schedule input at the public configuration boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    active_from: datetime | None = Field(
        default=None,
        description="Inclusive start of scheduled occurrences; timezone-aware",
    )
    active_until: datetime | None = Field(
        default=None,
        description="Exclusive end of scheduled occurrences; does not cancel admitted work",
    )

    @field_validator("active_from", "active_until")
    @classmethod
    def active_bound_is_aware(cls, value: datetime | None) -> datetime | None:
        """Normalize optional occurrence boundaries to UTC."""
        return None if value is None else _aware_utc(value, name="active boundary")

    @model_validator(mode="after")
    def active_period_is_ordered(self) -> Self:
        """Reject an empty or inverted active period."""
        if (
            self.active_from is not None
            and self.active_until is not None
            and self.active_from >= self.active_until
        ):
            raise ValueError("active_from must precede active_until")
        return self


class OnceSchedule(_ScheduleModel):
    """Run once at an explicit timezone-aware instant."""

    kind: Literal["once"] = "once"
    at: datetime = Field(description="Timezone-aware instant for the only run")

    @field_validator("at")
    @classmethod
    def at_is_aware(cls, value: datetime) -> datetime:
        """Normalize the configured instant to UTC."""

        return _aware_utc(value, name="at")


class IntervalSchedule(_ScheduleModel):
    """Run at a fixed rate anchored to an explicit instant."""

    kind: Literal["interval"] = "interval"
    every_seconds: int = Field(
        ge=MIN_INTERVAL_SECONDS,
        le=MAX_INTERVAL_SECONDS,
        description="Fixed interval in seconds",
    )
    start_at: datetime = Field(description="Timezone-aware fixed-rate anchor")

    @field_validator("start_at")
    @classmethod
    def start_at_is_aware(cls, value: datetime) -> datetime:
        """Normalize the fixed-rate anchor to UTC."""

        return _aware_utc(value, name="start_at")


class CronSchedule(_ScheduleModel):
    """Run from a five-field Cron expression in an IANA timezone.

    The fields are minute, hour, day of month, month, and day of week. Weekdays
    use ``mon`` through ``sun`` names so the public contract does not inherit a
    scheduler-specific numeric weekday convention.
    """

    kind: Literal["cron"] = "cron"
    expression: str = Field(
        min_length=9,
        max_length=256,
        description="Five-field Cron expression using named weekdays",
    )
    timezone: str = Field(
        min_length=1,
        max_length=128,
        description="IANA timezone used to interpret calendar fields",
    )

    @field_validator("expression")
    @classmethod
    def expression_has_five_fields(cls, value: str) -> str:
        """Reject dialects with seconds, years, or numeric weekdays."""

        if value != value.strip() or len(value.split()) != 5:
            raise ValueError("expression must contain exactly five Cron fields")
        weekday = value.split()[4].lower()
        if any(character.isdigit() for character in weekday):
            raise ValueError("day of week must use mon through sun names")
        normalized = value.lower()
        _compile_cron(normalized)
        return normalized

    @field_validator("timezone")
    @classmethod
    def timezone_is_available(cls, value: str) -> str:
        """Require an installed IANA timezone rather than a fixed abbreviation."""

        if value != value.strip():
            raise ValueError("timezone must not contain surrounding whitespace")
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise InvalidScheduleError(
                "Unknown IANA timezone",
                context={"timezone": value},
                cause=error,
            ) from error
        return value


Schedule: TypeAlias = Annotated[
    OnceSchedule | IntervalSchedule | CronSchedule,
    Field(discriminator="kind"),
]

_SCHEDULE_ADAPTER: TypeAdapter[Schedule] = TypeAdapter(Schedule)


@dataclass(frozen=True, slots=True)
class MaterializedSchedule:
    """Bounded due times and the first future wakeup after materialization."""

    due_at: tuple[datetime, ...]
    next_run_at: datetime | None
    skipped_before: datetime | None


def schedule_to_json(schedule: Schedule) -> str:
    """Encode a validated schedule as canonical JSON for durable storage."""

    return _SCHEDULE_ADAPTER.dump_json(schedule).decode("utf-8")


def schedule_from_json(value: str) -> Schedule:
    """Decode and validate one durable schedule value."""

    return _SCHEDULE_ADAPTER.validate_json(value)


def next_run_after(schedule: Schedule, after: datetime) -> datetime | None:
    """Return the first scheduled UTC instant strictly after ``after``.

    Cron calendar days are bounded to eight years. A local time inside a DST gap
    is skipped, while an overlap selects the earlier UTC candidate exactly once.

    Args:
        schedule: Validated schedule declaration.
        after: Timezone-aware exclusive lower bound.

    Returns:
        The first UTC run time, or ``None`` when a one-time schedule is exhausted.

    Raises:
        InvalidScheduleError: A Cron schedule has no supported future candidate.
        ValueError: ``after`` is timezone-naive.
    """

    after_utc = _aware_utc(after, name="after")
    if schedule.active_until is not None and after_utc >= schedule.active_until:
        return None
    if schedule.active_from is not None and after_utc < schedule.active_from:
        after_utc = schedule.active_from - timedelta(microseconds=1)
    candidate = _next_occurrence(schedule, after_utc)
    if (
        candidate is not None
        and schedule.active_until is not None
        and candidate >= schedule.active_until
    ):
        return None
    return candidate


def _next_occurrence(schedule: Schedule, after_utc: datetime) -> datetime | None:
    if isinstance(schedule, OnceSchedule):
        return schedule.at if schedule.at > after_utc else None
    if isinstance(schedule, IntervalSchedule):
        if after_utc < schedule.start_at:
            return schedule.start_at
        elapsed = (after_utc - schedule.start_at).total_seconds()
        steps = max(1, int(elapsed // schedule.every_seconds) + 1)
        return schedule.start_at + timedelta(seconds=steps * schedule.every_seconds)

    compiled = _compile_cron(schedule.expression)
    zone = ZoneInfo(schedule.timezone)
    local_after = after_utc.astimezone(zone)
    start_day = local_after.date()
    end_year = start_day.year + MAX_CRON_SEARCH_YEARS
    current_day = start_day
    while current_day.year <= end_year:
        if (
            schedule.active_until is not None
            and current_day > schedule.active_until.astimezone(zone).date()
        ):
            return None
        if (
            current_day.month in compiled.months
            and current_day.day in compiled.days
            and current_day.weekday() in compiled.weekdays
        ):
            for hour in sorted(compiled.hours):
                for minute in sorted(compiled.minutes):
                    local_value = datetime.combine(current_day, time(hour, minute))
                    candidates = _valid_utc_candidates(local_value, zone)
                    if candidates and candidates[0] > after_utc:
                        return candidates[0]
        current_day += timedelta(days=1)
    raise InvalidScheduleError(
        "Cron schedule has no run time within the supported search range",
        context={"timezone": schedule.timezone},
    )


def preview_schedule(
    schedule: Schedule,
    *,
    after: datetime,
    count: int = 5,
) -> tuple[datetime, ...]:
    """Return a bounded sequence of future UTC run times."""

    if not 1 <= count <= 100:
        raise ValueError("count must be between 1 and 100")
    cursor = _aware_utc(after, name="after")
    result: list[datetime] = []
    for _ in range(count):
        candidate = next_run_after(schedule, cursor)
        if candidate is None:
            break
        result.append(candidate)
        cursor = candidate
    return tuple(result)


def _latest_interval_due(
    schedule: IntervalSchedule,
    *,
    first_due: datetime,
    now: datetime,
) -> datetime:
    elapsed = (now - first_due).total_seconds()
    steps = max(0, int(elapsed // schedule.every_seconds))
    return first_due + timedelta(seconds=steps * schedule.every_seconds)


def materialize_schedule(
    schedule: Schedule,
    *,
    next_run_at: datetime,
    now: datetime,
    policy: MisfirePolicy,
) -> MaterializedSchedule:
    """Apply a bounded misfire policy to one persisted scheduler wakeup.

    The caller persists ``next_run_at`` atomically with the returned execution
    records. Queue waiting after materialization is governed by queue deadlines,
    not by this policy.
    """

    first_due = _aware_utc(next_run_at, name="next_run_at")
    now_utc = _aware_utc(now, name="now")
    if schedule.active_from is not None and first_due < schedule.active_from:
        candidate = next_run_after(schedule, first_due - timedelta(microseconds=1))
        if candidate is None:
            return MaterializedSchedule((), None, first_due)
        first_due = candidate
    if schedule.active_until is not None and first_due >= schedule.active_until:
        return MaterializedSchedule((), None, first_due)
    if first_due > now_utc:
        return MaterializedSchedule((), first_due, None)

    future = next_run_after(schedule, now_utc)
    grace_boundary = now_utc - policy.grace
    window_boundary = now_utc - policy.catch_up_window
    if policy.mode is MisfireMode.SKIP:
        due = () if first_due < grace_boundary else (first_due,)
        skipped = first_due if not due else None
        return MaterializedSchedule(due, future, skipped)

    if policy.mode is MisfireMode.LATEST and isinstance(schedule, IntervalSchedule):
        last_allowed = (
            now_utc
            if schedule.active_until is None
            else min(now_utc, schedule.active_until - timedelta(microseconds=1))
        )
        latest = _latest_interval_due(schedule, first_due=first_due, now=last_allowed)
        due = (latest,) if latest >= window_boundary else ()
        return MaterializedSchedule(
            due, future, first_due if latest != first_due else None
        )

    scan_start = max(first_due, window_boundary)
    cursor = scan_start - timedelta(microseconds=1)
    due_times: list[datetime] = []
    for _ in range(_MAX_MISFIRE_SCAN):
        candidate = next_run_after(schedule, cursor)
        if candidate is None or candidate > now_utc:
            break
        due_times.append(candidate)
        cursor = candidate
    else:
        raise InvalidScheduleError(
            "Misfire scan exceeded its bounded candidate limit",
            context={"limit": _MAX_MISFIRE_SCAN},
        )

    if policy.mode is MisfireMode.LATEST:
        selected = tuple(due_times[-1:])
    else:
        selected = tuple(due_times[: policy.max_catch_up])
    skipped = (
        first_due if first_due < scan_start or len(due_times) > len(selected) else None
    )
    return MaterializedSchedule(selected, future, skipped)


__all__ = [
    "MAX_CRON_SEARCH_YEARS",
    "MAX_INTERVAL_SECONDS",
    "MIN_INTERVAL_SECONDS",
    "CronSchedule",
    "IntervalSchedule",
    "MaterializedSchedule",
    "OnceSchedule",
    "Schedule",
    "materialize_schedule",
    "next_run_after",
    "preview_schedule",
    "schedule_from_json",
    "schedule_to_json",
]
