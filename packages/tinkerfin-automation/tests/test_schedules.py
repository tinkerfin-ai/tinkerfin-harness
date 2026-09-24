from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import TypeAdapter

from tinkerfin_automation import (
    CronSchedule,
    IntervalSchedule,
    MisfireMode,
    MisfirePolicy,
    OnceSchedule,
    Schedule,
    ScheduleSpec,
)
from tinkerfin_automation.schedules import (
    materialize_schedule,
    next_run_after,
    preview_schedule,
    schedule_from_json,
    schedule_to_json,
)


def test_once_and_interval_are_strictly_after_cursor() -> None:
    once_at = datetime(2026, 9, 10, 8, tzinfo=UTC)
    once = OnceSchedule(at=once_at)
    assert next_run_after(once, once_at - timedelta(microseconds=1)) == once_at
    assert next_run_after(once, once_at) is None

    interval = IntervalSchedule(every_seconds=60, start_at=once_at)
    assert next_run_after(interval, once_at) == once_at + timedelta(minutes=1)
    assert next_run_after(interval, once_at + timedelta(seconds=1)) == (
        once_at + timedelta(minutes=1)
    )


def test_factories_preserve_persisted_models_and_discriminator() -> None:
    anchor = datetime(2026, 9, 10, tzinfo=UTC)
    values = (
        Schedule.once(at=anchor),
        Schedule.every(minutes=1, start_at=anchor),
        Schedule.every(days=365, start_at=anchor),
        Schedule.cron("0 9 * * mon-fri", timezone="Asia/Shanghai"),
    )
    adapter = TypeAdapter(ScheduleSpec)
    for value in values:
        assert adapter.validate_json(adapter.dump_json(value)) == value
        assert schedule_from_json(schedule_to_json(value)) == value
    assert adapter.json_schema()["discriminator"]["propertyName"] == "kind"
    with pytest.raises(TypeError):
        Schedule()


@pytest.mark.parametrize("seconds", [-1, 0, 59, True, 60.5, 365 * 86400 + 1])
def test_interval_factory_rejects_invalid_duration(seconds) -> None:
    with pytest.raises(ValueError):
        Schedule.every(seconds=seconds, start_at=datetime(2026, 9, 10, tzinfo=UTC))


def test_cron_skips_dst_gap() -> None:
    schedule = CronSchedule(
        expression="30 2 * * *",
        timezone="America/New_York",
    )
    after = datetime(2026, 3, 8, 5, tzinfo=UTC)

    assert next_run_after(schedule, after) == datetime(2026, 3, 9, 6, 30, tzinfo=UTC)


def test_cron_overlap_selects_earlier_utc_candidate_once() -> None:
    schedule = CronSchedule(
        expression="30 1 * * *",
        timezone="America/New_York",
    )
    before_overlap = datetime(2026, 11, 1, 4, tzinfo=UTC)
    first = next_run_after(schedule, before_overlap)

    assert first == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert first is not None
    assert next_run_after(schedule, first) == datetime(2026, 11, 2, 6, 30, tzinfo=UTC)


def test_named_weekdays_and_months_are_supported() -> None:
    schedule = CronSchedule(
        expression="0 8 * jan mon-fri",
        timezone="Asia/Shanghai",
    )
    assert preview_schedule(
        schedule,
        after=datetime(2026, 1, 1, tzinfo=UTC),
        count=2,
    ) == (
        datetime(2026, 1, 2, 0, tzinfo=UTC),
        datetime(2026, 1, 5, 0, tzinfo=UTC),
    )


def test_numeric_weekday_and_invalid_timezone_are_rejected() -> None:
    with pytest.raises(ValueError, match="day of week"):
        CronSchedule(expression="0 8 * * 1", timezone="UTC")
    with pytest.raises(ValueError, match="Unknown IANA timezone"):
        CronSchedule(expression="0 8 * * mon", timezone="Mars/Olympus")


def test_schedule_json_round_trip_preserves_variant() -> None:
    schedule = CronSchedule(expression="*/15 8-10 * * mon", timezone="UTC")
    restored = schedule_from_json(schedule_to_json(schedule))
    assert restored == schedule
    assert isinstance(restored, CronSchedule)


def test_misfire_latest_uses_original_fixed_rate_time() -> None:
    start = datetime(2026, 9, 9, 8, tzinfo=UTC)
    schedule = IntervalSchedule(every_seconds=60, start_at=start)
    materialized = materialize_schedule(
        schedule,
        next_run_at=start,
        now=start + timedelta(minutes=5, seconds=20),
        policy=MisfirePolicy(mode=MisfireMode.LATEST),
    )

    assert materialized.due_at == (start + timedelta(minutes=5),)
    assert materialized.next_run_at == start + timedelta(minutes=6)


def test_misfire_catch_up_is_bounded() -> None:
    start = datetime(2026, 9, 9, 8, tzinfo=UTC)
    schedule = IntervalSchedule(every_seconds=60, start_at=start)
    materialized = materialize_schedule(
        schedule,
        next_run_at=start,
        now=start + timedelta(minutes=5),
        policy=MisfirePolicy(mode=MisfireMode.CATCH_UP, max_catch_up=3),
    )

    assert materialized.due_at == (
        start,
        start + timedelta(minutes=1),
        start + timedelta(minutes=2),
    )
    assert materialized.skipped_before == start


def test_misfire_skip_respects_grace() -> None:
    due = datetime(2026, 9, 9, 8, tzinfo=UTC)
    schedule = OnceSchedule(at=due)
    policy = MisfirePolicy(mode=MisfireMode.SKIP, grace=timedelta(seconds=60))

    assert materialize_schedule(
        schedule,
        next_run_at=due,
        now=due + timedelta(seconds=30),
        policy=policy,
    ).due_at == (due,)
    assert not materialize_schedule(
        schedule,
        next_run_at=due,
        now=due + timedelta(seconds=61),
        policy=policy,
    ).due_at
