"""Public five-operation Trace Ledger backend and framework Store contracts."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from tinkerfin_contracts import RunIdentity, ThreadIdentity
from tinkerfin_tracing import (
    DurableTraceStore,
    InMemoryTraceStore,
    TraceLedgerBackend,
    TraceProjectionCheckpoint,
    TraceStore,
    TraceStoreOptions,
    TraceStoreProtocolError,
    TraceThreadKey,
    TraceThreadNotFound,
    verify_trace_ledger_backend,
)
from tinkerfin_tracing.backend import (
    StoredTraceEventPage,
    TraceCheckpointRequest,
    TraceEventPageRequest,
    TraceLedgerChange,
    TraceLedgerStateRequest,
    resolve_ledger_change,
)
from tinkerfin_tracing.durable_store import _InMemoryTraceLedgerBackend
from tinkerfin_tracing.facts import RunFact


def test_trace_ledger_backend_has_five_descriptive_operations() -> None:
    operations = {
        name
        for name, value in inspect.getmembers(TraceLedgerBackend)
        if inspect.isfunction(value) and not name.startswith("_")
    }
    assert callable(resolve_ledger_change)

    assert operations == {
        "prepare_storage",
        "commit_ledger_change",
        "load_ledger_state",
        "read_event_page",
        "load_projection_checkpoint",
    }


def test_durable_store_rejects_incomplete_backend_at_construction() -> None:
    class IncompleteBackend:
        async def prepare_storage(self) -> None:
            pass

    with pytest.raises(TypeError, match="TraceLedgerBackend"):
        DurableTraceStore(IncompleteBackend())  # type: ignore[arg-type]


def test_in_memory_store_uses_the_complete_framework_store_contract() -> None:
    store = InMemoryTraceStore()

    assert isinstance(store, DurableTraceStore)
    assert isinstance(store, TraceStore)
    assert isinstance(store.backend, TraceLedgerBackend)


def test_trace_store_options_use_storage_role_names() -> None:
    assert set(TraceStoreOptions.__dataclass_fields__) == {
        "writer_lease_seconds",
        "writer_heartbeat_interval_seconds",
        "follow_poll_seconds",
        "commit_retry_attempts",
        "commit_retry_delay_seconds",
    }


@pytest.mark.parametrize(
    "options",
    (None, TraceStoreOptions(follow_poll_seconds=0.01)),
    ids=("defaults", "explicit-options"),
)
async def test_public_backend_verifier_exercises_independent_shared_instances(
    monkeypatch: pytest.MonkeyPatch,
    options: TraceStoreOptions | None,
) -> None:
    primary = _InMemoryTraceLedgerBackend()
    peer = primary._shared_peer()
    observed: list[TraceStoreOptions] = []
    original = primary.commit_ledger_change

    async def record_options(change: TraceLedgerChange):
        observed.append(change.options)
        return await original(change)

    monkeypatch.setattr(primary, "commit_ledger_change", record_options)

    await verify_trace_ledger_backend(primary, peer, options=options)

    assert observed
    assert all(value == (options or TraceStoreOptions()) for value in observed)


async def test_backend_verifier_cleans_first_writer_when_peer_open_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = _InMemoryTraceLedgerBackend()
    peer = primary._shared_peer()
    original = peer.commit_ledger_change

    async def fail_second_open(change: TraceLedgerChange):
        if (
            change.kind == "open_writer"
            and change.identity is not None
            and change.identity.run_id == "contract-second"
        ):
            raise RuntimeError("injected second open failure")
        return await original(change)

    monkeypatch.setattr(peer, "commit_ledger_change", fail_second_open)
    namespace = "failed-peer-open-contract"

    with pytest.raises(RuntimeError, match="injected second open failure"):
        await verify_trace_ledger_backend(primary, peer, namespace=namespace)

    await asyncio.sleep(0)
    assert not any(key[0] == namespace for key in primary._threads)
    assert not any(
        task.get_name().startswith("tinkerfin-trace-ledger-heartbeat:")
        for task in asyncio.all_tasks()
        if not task.done()
    )


async def test_backend_verifier_cancels_follower_when_third_append_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = _InMemoryTraceLedgerBackend()
    peer = primary._shared_peer()
    original = primary.commit_ledger_change

    async def fail_third_append(change: TraceLedgerChange):
        if change.kind == "append_events" and change.run_id == "contract-third":
            raise RuntimeError("injected third append failure")
        return await original(change)

    monkeypatch.setattr(primary, "commit_ledger_change", fail_third_append)
    namespace = "failed-third-append-contract"

    with pytest.raises(RuntimeError, match="injected third append failure"):
        await verify_trace_ledger_backend(primary, peer, namespace=namespace)

    await asyncio.sleep(0)
    assert not any(key[0] == namespace for key in primary._threads)
    assert not any(
        task.get_name().startswith("tinkerfin-trace-ledger-heartbeat:")
        for task in asyncio.all_tasks()
        if not task.done()
    )


@pytest.mark.parametrize("direction", ["forward", "reverse"])
@pytest.mark.parametrize("foreign_component", ["thread_id", "generation"])
async def test_store_rejects_event_page_for_another_exact_generation(
    monkeypatch: pytest.MonkeyPatch,
    direction: str,
    foreign_component: str,
) -> None:
    store = InMemoryTraceStore()
    identity = RunIdentity(
        namespace="test", thread_id="requested-thread", run_id="requested-run"
    )
    writer = await store.open_writer(identity)
    await writer.append(
        (
            RunFact(
                source_observation_id="requested-started",
                identity=identity,
                occurred_at=datetime.now(UTC),
                monotonic_ns=1,
                phase="started",
                input_kind="ordinary",
            ),
        )
    )
    snapshot = await store.snapshot(identity.thread)
    backend = store.backend
    assert isinstance(backend, _InMemoryTraceLedgerBackend)
    original = backend.read_event_page

    async def return_foreign_page(
        request: TraceEventPageRequest,
    ) -> StoredTraceEventPage:
        page = await original(request)
        update = (
            {"thread_id": "foreign-thread"}
            if foreign_component == "thread_id"
            else {"generation": "foreign-generation"}
        )
        foreign_key = TraceThreadKey(
            namespace=page.key.namespace,
            thread_id=update.get("thread_id", page.key.thread_id),
            generation=update.get("generation", page.key.generation),
        )
        return replace(
            page,
            key=foreign_key,
            tail_seq=0 if direction == "reverse" else page.tail_seq,
        )

    monkeypatch.setattr(backend, "read_event_page", return_foreign_page)
    try:
        with pytest.raises(TraceStoreProtocolError, match="requested generation"):
            if direction == "forward":
                await store.read_events(
                    snapshot.key,
                    after_seq=0,
                    as_of_seq=1,
                    limit=1,
                )
            else:
                await store.read_events_reverse(
                    snapshot.key,
                    before_seq=2,
                    limit=1,
                )
    finally:
        await writer.aclose()


async def test_store_rejects_checkpoint_for_another_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryTraceStore()
    identity = RunIdentity(
        namespace="test", thread_id="checkpoint-thread", run_id="checkpoint-run"
    )
    writer = await store.open_writer(identity)
    checkpoint = TraceProjectionCheckpoint(
        key=writer.key,
        projection_name="requested.checkpoint",
        run_id=None,
        as_of_seq=0,
        state={"count": 0},
    )
    await store.save_projection_checkpoint(checkpoint, expected_as_of_seq=None)
    backend = store.backend
    assert isinstance(backend, _InMemoryTraceLedgerBackend)
    original = backend.load_projection_checkpoint

    async def return_foreign_checkpoint(request: TraceCheckpointRequest):
        stored = await original(request)
        assert stored is not None
        return replace(stored, projection_name="foreign.checkpoint", as_of_seq=99)

    monkeypatch.setattr(
        backend,
        "load_projection_checkpoint",
        return_foreign_checkpoint,
    )
    try:
        with pytest.raises(TraceStoreProtocolError, match="requested scope"):
            await store.load_projection_checkpoint(
                writer.key,
                projection_name="requested.checkpoint",
                run_id=None,
                as_of_seq=0,
            )
    finally:
        await writer.aclose()


async def test_snapshot_rejects_backend_state_for_another_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryTraceStore()
    identity = RunIdentity(
        namespace="test", thread_id="requested-thread", run_id="requested-run"
    )
    writer = await store.open_writer(identity)
    backend = store.backend
    assert isinstance(backend, _InMemoryTraceLedgerBackend)
    original = backend.load_ledger_state

    async def return_foreign_state(request: TraceLedgerStateRequest):
        state = await original(request)
        assert state.thread is not None
        foreign_key = TraceThreadKey(
            namespace=state.thread.key.namespace,
            thread_id="foreign-thread",
            generation=state.thread.key.generation,
        )
        return replace(state, thread=replace(state.thread, key=foreign_key))

    monkeypatch.setattr(backend, "load_ledger_state", return_foreign_state)
    try:
        with pytest.raises(TraceThreadNotFound):
            await store.snapshot(identity.thread)
    finally:
        await writer.aclose()


async def test_forward_event_page_rejects_descending_sequences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryTraceStore()
    identity = RunIdentity(
        namespace="test", thread_id="ordered-thread", run_id="ordered-run"
    )
    writer = await store.open_writer(identity)
    await writer.append(
        tuple(
            RunFact(
                source_observation_id=f"ordered-{index}",
                identity=identity,
                occurred_at=datetime.now(UTC),
                monotonic_ns=index,
                phase="started",
                input_kind="ordinary",
            )
            for index in (1, 2)
        )
    )
    snapshot = await store.snapshot(identity.thread)
    backend = store.backend
    assert isinstance(backend, _InMemoryTraceLedgerBackend)
    original = backend.read_event_page

    async def reverse_page(request: TraceEventPageRequest) -> StoredTraceEventPage:
        page = await original(request)
        return replace(page, events=tuple(reversed(page.events)))

    monkeypatch.setattr(backend, "read_event_page", reverse_page)
    try:
        with pytest.raises(TraceStoreProtocolError, match="ordering"):
            await store.read_events(
                snapshot.key,
                after_seq=0,
                as_of_seq=2,
                limit=2,
            )
    finally:
        await writer.aclose()


async def test_store_rejects_commit_result_for_another_writer_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _InMemoryTraceLedgerBackend()
    original = backend.commit_ledger_change

    async def return_foreign_writer(change: TraceLedgerChange):
        result = await original(change)
        if change.kind != "open_writer" or result.key is None:
            return result
        foreign_key = TraceThreadKey(
            namespace=result.key.namespace,
            thread_id="foreign-thread",
            generation=result.key.generation,
        )
        return replace(result, key=foreign_key, run_id="foreign-run")

    monkeypatch.setattr(backend, "commit_ledger_change", return_foreign_writer)
    store = DurableTraceStore(backend)
    try:
        with pytest.raises(TraceStoreProtocolError, match="writer result"):
            await store.open_writer(
                RunIdentity(
                    namespace="test",
                    thread_id="requested-thread",
                    run_id="requested-run",
                )
            )
    finally:
        backend._threads.clear()


async def test_store_rejects_append_result_with_foreign_event_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _InMemoryTraceLedgerBackend()
    store = DurableTraceStore(backend)
    identity = RunIdentity(
        namespace="test", thread_id="append-thread", run_id="append-run"
    )
    writer = await store.open_writer(identity)
    original = backend.commit_ledger_change

    async def return_foreign_event(change: TraceLedgerChange):
        result = await original(change)
        if change.kind != "append_events" or not result.events:
            return result
        foreign = result.events[0].model_copy(
            update={"generation": "foreign-generation", "trace_seq": 999}
        )
        return replace(result, events=(foreign,))

    monkeypatch.setattr(backend, "commit_ledger_change", return_foreign_event)
    try:
        with pytest.raises(TraceStoreProtocolError, match="committed event"):
            await writer.append(
                (
                    RunFact(
                        source_observation_id="append-started",
                        identity=identity,
                        occurred_at=datetime.now(UTC),
                        monotonic_ns=1,
                        phase="started",
                        input_kind="ordinary",
                    ),
                )
            )
    finally:
        monkeypatch.setattr(backend, "commit_ledger_change", original)
        await writer.aclose()


async def test_writer_rejects_empty_ordinary_and_mandatory_batches() -> None:
    store = InMemoryTraceStore()
    writer = await store.open_writer(
        RunIdentity(namespace="test", thread_id="empty-thread", run_id="empty-run")
    )
    try:
        with pytest.raises(ValueError, match="must not be empty"):
            await writer.append(())
        with pytest.raises(ValueError, match="must not be empty"):
            await writer.append((), mandatory=True)
        assert (
            await store.snapshot(
                ThreadIdentity(namespace="test", thread_id="empty-thread")
            )
        ).as_of_seq == 0
    finally:
        await writer.aclose()


async def test_checkpoint_result_and_payload_share_one_deep_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _InMemoryTraceLedgerBackend()
    store = DurableTraceStore(backend)
    writer = await store.open_writer(
        RunIdentity(
            namespace="test", thread_id="checkpoint-snapshot", run_id="checkpoint-run"
        )
    )
    checkpoint = TraceProjectionCheckpoint(
        key=writer.key,
        projection_name="snapshot.checkpoint",
        run_id=None,
        as_of_seq=0,
        state={"value": 1},
    )
    original = backend.commit_ledger_change
    commit_started = asyncio.Event()
    release_commit = asyncio.Event()

    async def delay_checkpoint(change: TraceLedgerChange):
        if change.kind == "save_projection_checkpoint":
            commit_started.set()
            await release_commit.wait()
        return await original(change)

    monkeypatch.setattr(backend, "commit_ledger_change", delay_checkpoint)
    saving = asyncio.create_task(
        store.save_projection_checkpoint(checkpoint, expected_as_of_seq=None)
    )
    try:
        await commit_started.wait()
        assert isinstance(checkpoint.state, dict)
        checkpoint.state["value"] = 2
        release_commit.set()
        returned = await saving
        loaded = await store.load_projection_checkpoint(
            writer.key,
            projection_name="snapshot.checkpoint",
            run_id=None,
            as_of_seq=0,
        )

        assert returned.state == {"value": 1}
        assert loaded is not None
        assert loaded.state == {"value": 1}
    finally:
        release_commit.set()
        if not saving.done():
            saving.cancel()
            await asyncio.gather(saving, return_exceptions=True)
        await writer.aclose()
