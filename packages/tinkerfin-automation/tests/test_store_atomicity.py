"""Keep rejected commands atomic and claims inside their full ownership scope."""

from dataclasses import replace
from datetime import timedelta

import pytest
from test_store_contract import _taskless_execution

from tinkerfin_automation import (
    ClaimLostError,
    ExecutionStatus,
)
from tinkerfin_automation.clock import ManualClock
from tinkerfin_automation.store import AutomationStore
from tinkerfin_contracts import RunIdentity


async def test_claim_recovery_only_changes_the_selected_namespace(
    store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, clock = store_with_clock
    execution = replace(
        _taskless_execution(execution_id="other-run"),
        namespace="other",
        identity=RunIdentity(namespace="other", thread_id="thread", run_id="run"),
    )
    await store.enqueue_execution(
        execution, occurrence_key="occurrence", request_id=None, input_digest="run"
    )
    (claim,) = (
        await store.claim_work(
            "other",
            "worker",
            limit=1,
            lease_duration=timedelta(minutes=1),
            global_concurrency=1,
        )
    ).claims
    started = await store.authorize_start(claim, execution_timeout=timedelta(hours=1))
    await clock.advance(timedelta(minutes=2))
    assert not (
        await store.claim_work(
            "app",
            "worker",
            limit=1,
            lease_duration=timedelta(minutes=1),
            global_concurrency=1,
        )
    ).claims
    assert (
        await store.get_execution("other", "owner-1", execution.execution_id)
        == started.execution
    )
    assert not (
        await store.claim_work(
            "other",
            "worker",
            limit=1,
            lease_duration=timedelta(minutes=1),
            global_concurrency=1,
        )
    ).claims
    assert (
        await store.get_execution("other", "owner-1", execution.execution_id)
    ).status is ExecutionStatus.NEEDS_ATTENTION


async def test_claim_cannot_authorize_an_execution_different_from_its_persisted_identity(
    store_with_clock: tuple[AutomationStore, ManualClock],
) -> None:
    store, _ = store_with_clock
    execution = _taskless_execution(execution_id="actual")
    await store.enqueue_execution(
        execution, occurrence_key="run", request_id=None, input_digest="run"
    )
    (claim,) = (
        await store.claim_work(
            "app",
            "worker",
            limit=1,
            lease_duration=timedelta(minutes=1),
            global_concurrency=1,
        )
    ).claims
    with pytest.raises(ClaimLostError):
        await store.authorize_start(
            replace(claim, execution_id="different"),
            execution_timeout=timedelta(minutes=1),
        )
    assert (
        await store.get_execution("app", "owner-1", execution.execution_id) == execution
    )
    assert (
        await store.authorize_start(claim, execution_timeout=timedelta(minutes=1))
    ).execution.identity == execution.identity
