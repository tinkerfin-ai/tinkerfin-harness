"""Reject malformed boundary inputs without changing persisted Automation facts.

Parameterized invalid inputs deliberately remain untyped until the public API
validates them; the production signatures retain their precise accepted types.
"""

import json
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from pydantic import JsonValue
from test_store_contract import NOW, _task, _taskless_execution

from tinkerfin_automation import (
    AttentionResolution,
    AutomationStoreProtocolError,
    ExecutionFailure,
    ExecutionStatus,
    MemoryAutomationStore,
    OnceSchedule,
)
from tinkerfin_automation._codec import decode_task, encode_task
from tinkerfin_automation.clock import ManualClock
from tinkerfin_automation.service import AutomationService
from tinkerfin_automation.store import AutomationStore, ScheduledExecution
from tinkerfin_contracts import RunIdentity


@pytest.mark.parametrize(
    ("value", "error"),
    [
        (b"automation", TypeError),
        ("automation\x00tenant", ValueError),
        ("automation\ud800", ValueError),
    ],
)
def test_service_rejects_non_string_or_nul_namespace(
    value: Any, error: type[Exception]
) -> None:
    with pytest.raises(error):
        AutomationService(namespace=value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("owner_id", b"owner", TypeError),
        ("name", "task\x00name", ValueError),
        ("target", b"target", TypeError),
        ("request_id", "request\x00id", ValueError),
    ],
)
async def test_service_rejects_invalid_identifier_inputs(
    field: str, value: Any, error: type[Exception]
) -> None:
    clock = ManualClock(NOW)
    service = AutomationService(namespace="app", clock=clock)
    try:
        schedule = OnceSchedule(at=NOW + timedelta(hours=1))
        with pytest.raises(error):
            if field == "owner_id":
                await service.create_task(
                    owner_id=value,
                    name="Task",
                    schedule=schedule,
                    target="target",
                    request_id="request",
                )
            elif field == "name":
                await service.create_task(
                    owner_id="owner",
                    name=value,
                    schedule=schedule,
                    target="target",
                    request_id="request",
                )
            elif field == "target":
                await service.create_task(
                    owner_id="owner",
                    name="Task",
                    schedule=schedule,
                    target=value,
                    request_id="request",
                )
            else:
                await service.create_task(
                    owner_id="owner",
                    name="Task",
                    schedule=schedule,
                    target="target",
                    request_id=value,
                )
    finally:
        await service.close()


@pytest.mark.parametrize(
    "value",
    [b"owner", " owner", "owner\x00id", "owner\ud800"],
)
def test_persisted_task_rejects_nonportable_identifiers(value: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        replace(_task(), owner_id=value)
    with pytest.raises((TypeError, ValueError)):
        replace(_taskless_execution(execution_id="run"), owner_id=value)


@pytest.mark.asyncio
async def test_control_operations_validate_owner_before_store_access() -> None:
    service = AutomationService(namespace="app")
    invalid_owner: Any = b"owner"
    try:
        with pytest.raises(TypeError):
            await service.cancel_execution(
                owner_id=invalid_owner,
                execution_id="execution",
                request_id="cancel",
            )
        with pytest.raises(ValueError):
            await service.resolve_execution(
                owner_id=" owner",
                execution_id="execution",
                resolution=AttentionResolution.FAILED,
                reason="operator-confirmed",
                request_id="resolve",
            )
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [b"owner", " owner", "owner\x00id", "owner\ud800"])
async def test_store_implementations_reject_invalid_direct_scope(
    store_with_clock: tuple[AutomationStore, ManualClock], value: Any
) -> None:
    store, _ = store_with_clock
    with pytest.raises((TypeError, ValueError)):
        await store.get_task("app", value, "task")
    with pytest.raises((TypeError, ValueError)):
        await store.list_tasks(
            "app",
            value,
            limit=10,
            cursor=None,
        )
    with pytest.raises((TypeError, ValueError)):
        await store.get_execution(
            "app",
            value,
            "execution",
        )
    with pytest.raises((TypeError, ValueError)):
        await store.list_executions(
            "app",
            value,
            task_id=None,
            limit=10,
            cursor=None,
        )
    with pytest.raises((TypeError, ValueError)):
        await store.cancel_execution(
            "app",
            value,
            "execution",
            request_id=None,
            input_digest="cancel",
        )
    with pytest.raises((TypeError, ValueError)):
        await store.resolve_execution(
            "app",
            value,
            "execution",
            resolution=AttentionResolution.FAILED,
            request_id="resolve",
            input_digest="resolve",
            reason="operator-confirmed",
        )
    with pytest.raises((TypeError, ValueError)):
        await store.get_scheduled_task(
            value,
            "task",
        )
    with pytest.raises((TypeError, ValueError)):
        await store.list_scheduled_tasks(
            value,
            limit=10,
            cursor=None,
        )
    with pytest.raises((TypeError, ValueError)):
        await store.get_scheduled_execution(
            value,
            "execution",
        )
    with pytest.raises((TypeError, ValueError)):
        await store.claim_work(
            "app",
            value,
            limit=1,
            lease_duration=timedelta(minutes=1),
            global_concurrency=1,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason", [b"reason", " reason", "reason\x00text", "reason\ud800"]
)
async def test_store_implementations_reject_invalid_resolution_reason(
    store_with_clock: tuple[AutomationStore, ManualClock], reason: Any
) -> None:
    store, _ = store_with_clock
    with pytest.raises((TypeError, ValueError)):
        await store.resolve_execution(
            "app",
            "owner",
            "execution",
            resolution=AttentionResolution.FAILED,
            request_id="resolve",
            input_digest="resolve",
            reason=reason,
        )


@pytest.mark.parametrize("number", [float("nan"), float("inf"), -float("inf")])
def test_task_and_execution_snapshots_reject_nonfinite_json(number: float) -> None:
    with pytest.raises(ValueError):
        replace(_task(), input={"nested": [number]})
    with pytest.raises(ValueError):
        replace(_taskless_execution(execution_id="run"), result={"nested": number})
    payload = json.loads(encode_task(_task()))
    payload["input"] = {"nested": [number]}
    with pytest.raises(AutomationStoreProtocolError):
        decode_task(json.dumps(payload))


@pytest.mark.parametrize(
    ("value", "error"),
    [
        (b"raw", TypeError),
        ({"nested": ("tuple",)}, TypeError),
        ({"nested": "lone\ud800"}, ValueError),
        ({1: "non-string key"}, TypeError),
    ],
)
def test_task_and_execution_snapshots_reject_non_json_values(
    value: Any, error: type[Exception]
) -> None:
    with pytest.raises(error):
        replace(_task(), input={"payload": value})
    with pytest.raises(error):
        replace(_taskless_execution(execution_id="run"), result={"payload": value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_id", "x" * 37),
        ("namespace", "x" * 129),
        ("execution_namespace", "x" * 129),
        ("owner_id", "x" * 192),
        ("name", "x" * 256),
        ("target", "x" * 192),
    ],
)
def test_persisted_models_reject_sql_text_width_overflows(
    field: str, value: str
) -> None:
    with pytest.raises(ValueError):
        invalid_fields: dict[str, Any] = {field: value}
        replace(_task(), **invalid_fields)


def test_execution_runtime_scope_is_independent_of_scheduling_scope() -> None:
    execution = replace(
        _taskless_execution(execution_id="run"),
        identity=RunIdentity(namespace="other", thread_id="thread", run_id="run"),
    )
    assert execution.namespace == "app"
    assert execution.identity.namespace == "other"


def test_task_codec_requires_and_preserves_execution_namespace() -> None:
    task = replace(_task(), execution_namespace="runtime-user")
    assert decode_task(encode_task(task)) == task
    payload = json.loads(encode_task(task))
    del payload["execution_namespace"]
    with pytest.raises(AutomationStoreProtocolError):
        decode_task(json.dumps(payload))


@pytest.mark.parametrize("value", [b"failure", "failure\x00text", "failure\ud800"])
def test_failure_values_reject_invalid_text(value: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        replace(
            _taskless_execution(execution_id="run"),
            failure_message=value,
        )
    with pytest.raises((TypeError, ValueError)):
        ExecutionFailure(code=value, message="failure")


async def test_service_rejects_nonfinite_json_before_persisting_execution() -> None:
    clock = ManualClock(NOW)
    store = MemoryAutomationStore(clock=clock)
    service = AutomationService(namespace="app", clock=clock, store=store)
    try:
        with pytest.raises(ValueError):
            await service.execute_once(
                owner_id="owner",
                target="target",
                input={"nested": [float("nan")]},
                request_id="nan",
            )
        assert not (
            await store.list_executions(
                "app", "owner", task_id=None, limit=100, cursor=None
            )
        ).items
    finally:
        await service.close()
        await store.close()


async def test_nonfinite_completion_keeps_the_claim_and_execution_unchanged(
    store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, _ = store_with_clock
    execution = _taskless_execution(execution_id="run")
    await store.enqueue_execution(
        execution, occurrence_key="run", request_id=None, input_digest="run"
    )
    (claim,) = await store.claim_work(
        "app",
        "worker",
        limit=1,
        lease_duration=timedelta(minutes=1),
        global_concurrency=1,
    )
    with pytest.raises(ValueError):
        await store.finish_execution(
            claim, status=ExecutionStatus.SUCCEEDED, result={"nested": [float("nan")]}
        )
    assert await store.get_execution("app", "owner-1", "run") == execution
    result: JsonValue = {"nul\x00key": [10**100, 1.25, True, None]}
    assert (
        await store.finish_execution(
            claim, status=ExecutionStatus.SUCCEEDED, result=result
        )
    ).result == result
    assert (await store.get_execution("app", "owner-1", "run")).result == result


async def test_schedule_batch_deduplicates_occurrences_before_queue_admission(
    store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, _ = store_with_clock
    task = _task()
    await store.create_task(task, request_id=None, input_digest="task")
    item = ScheduledExecution(
        execution=_taskless_execution(execution_id="one"), occurrence_key="occurrence"
    )
    item = replace(item, execution=replace(item.execution, task_id=task.task_id))
    second = replace(item, execution=replace(item.execution, execution_id="second"))
    assert task.next_run_at is not None
    materialized = await store.materialize_task(
        replace(task, next_run_at=None),
        expected_next_run_at=task.next_run_at,
        executions=(item, second),
    )
    assert materialized.executions == (item.execution,)
    assert (
        await store.list_executions(
            "app", "owner-1", task_id=task.task_id, limit=100, cursor=None
        )
    ).items == (item.execution,)
