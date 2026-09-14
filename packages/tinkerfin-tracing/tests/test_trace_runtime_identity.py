"""Keep shared Trace storage and queries isolated by Runtime-owned identities."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Literal

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tinkerfin_contracts import (
    RunClosedObservation,
    RunIdentity,
    RunInputObservation,
    RunSourceContext,
    RunStartedObservation,
    RunTerminalObservation,
    ThreadIdentity,
)
from tinkerfin_tracing import (
    CanonicalTracePayloadCodec,
    InMemoryTraceStore,
    RunFact,
    SqlAlchemyTraceStore,
    TraceLimits,
    TraceProjectionCheckpoint,
    TraceQuotaExceeded,
    Tracer,
    TraceStoreError,
    TraceStoreProtocolError,
    TraceThreadNotFound,
)
from tinkerfin_tracing.backend import (
    StoredTraceEvent,
    StoredTraceEventPage,
    TraceEventPageRequest,
    TraceLedgerChange,
    TraceLedgerStateRequest,
    resolve_ledger_change,
)
from tinkerfin_tracing.store import StoreThreadSnapshot, TraceWriter

_NOW = datetime(2026, 9, 10, 1, 2, 3, 123456, tzinfo=UTC)


def _identity(namespace: str = "alpha", run_id: str = "run") -> RunIdentity:
    return RunIdentity(namespace=namespace, thread_id="same-thread", run_id=run_id)


def _fact(identity: RunIdentity) -> RunFact:
    return RunFact(
        identity=identity,
        phase="started",
        input_kind="ordinary",
        occurred_at=_NOW,
        monotonic_ns=1,
        source_observation_id="same-observation",
    )


async def _record(tracer: Tracer, identity: RunIdentity) -> None:
    source = RunSourceContext(
        identity=identity,
        runtime_profile="deepagents-v2",
        input_kind="ordinary",
        input={
            "messages": [
                {"role": "user", "id": "same-message", "content": identity.namespace}
            ]
        },
        config={},
    )
    session = await tracer.open_run(source)
    try:
        await session.observe(
            RunStartedObservation(identity=identity, observed_at=_NOW, monotonic_ns=1)
        )
        await session.observe(
            RunInputObservation(
                identity=identity, source=source, observed_at=_NOW, monotonic_ns=2
            )
        )
        await session.observe(
            RunTerminalObservation(
                identity=identity, outcome="succeeded", observed_at=_NOW, monotonic_ns=3
            )
        )
        await session.observe(
            RunClosedObservation(
                identity=identity, outcome="succeeded", observed_at=_NOW, monotonic_ns=4
            )
        )
    finally:
        await session.aclose()


async def test_one_store_accepts_identical_runs_in_distinct_runtime_namespaces() -> (
    None
):
    store = InMemoryTraceStore()
    writers: list[TraceWriter] = []
    try:
        for namespace in ("user-a", "user-b"):
            writers.append(
                await store.open_writer(
                    RunIdentity(
                        namespace=namespace,
                        thread_id="same-thread",
                        run_id="same-run",
                    )
                )
            )
        assert [writer.key.namespace for writer in writers] == ["user-a", "user-b"]
        assert writers[0].key != writers[1].key
    finally:
        for writer in writers:
            await writer.aclose()


async def test_sql_writer_preserves_opaque_utf8_run_and_thread_ids(
    trace_sql_engine: AsyncEngine,
) -> None:
    store = SqlAlchemyTraceStore(trace_sql_engine)
    identity = RunIdentity(
        namespace="scope", thread_id="thread\x00字", run_id="run\x00字"
    )
    writer = await store.open_writer(identity)
    try:
        assert writer.key.thread_id == identity.thread_id
        assert writer.run_id == identity.run_id
    finally:
        await writer.aclose()


async def test_shared_store_namespaces_keep_writers_quotas_and_deletion_independent(
    trace_sql_engine: AsyncEngine,
) -> None:
    limits = TraceLimits(max_tracer_threads=1)
    for store in (
        InMemoryTraceStore(limits=limits),
        SqlAlchemyTraceStore(trace_sql_engine, limits=limits),
    ):
        identities = (_identity(), _identity("beta"))
        writers: list[TraceWriter] = []
        try:
            for identity in identities:
                writers.append(await store.open_writer(identity))
            for writer, identity in zip(writers, identities, strict=True):
                assert writer.key.thread == identity.thread
                assert (await writer.append((_fact(identity),)))[0].trace_seq == 1
                other = RunIdentity(
                    namespace=identity.namespace,
                    thread_id="second-thread",
                    run_id="run",
                )
                with pytest.raises(TraceQuotaExceeded):
                    await store.open_writer(other)
            with pytest.raises(TraceStoreProtocolError, match="bound Run"):
                await writers[0].append((_fact(identities[0]), _fact(identities[1])))
            assert (await store.snapshot(identities[0].thread)).as_of_seq == 1
            assert (await store.snapshot(identities[1].thread)).as_of_seq == 1
        finally:
            for writer in writers:
                await writer.aclose()
        alpha, beta = writers
        await store.delete(alpha.key)
        with pytest.raises(TraceThreadNotFound):
            await store.snapshot(identities[0].thread)
        assert (await store.snapshot_key(beta.key)).as_of_seq == 1
        replacement = await store.open_writer(identities[0])
        try:
            assert replacement.key.generation not in {
                alpha.key.generation,
                beta.key.generation,
            }
        finally:
            await replacement.aclose()


async def test_sql_opaque_text_roundtrip_preserves_original_writer_order_and_checkpoints(
    trace_sql_engine: AsyncEngine,
) -> None:
    identity = RunIdentity(
        namespace="ns\x00字", thread_id="thread\x00字", run_id="a\x00字"
    )
    second = RunIdentity(
        namespace=identity.namespace, thread_id=identity.thread_id, run_id="a 字"
    )
    for store in (InMemoryTraceStore(), SqlAlchemyTraceStore(trace_sql_engine)):
        writers = [await store.open_writer(identity), await store.open_writer(second)]
        try:
            committed = await writers[0].append((_fact(identity),))
            snapshot = await store.snapshot(identity.thread)
            assert snapshot.active_run_ids == (identity.run_id, second.run_id)
            events = await store.read_events(
                snapshot.key, after_seq=0, as_of_seq=1, limit=1
            )
            assert events == committed
            for run_id in (None, identity.run_id):
                checkpoint = TraceProjectionCheckpoint(
                    key=snapshot.key,
                    projection_name="projection\x00字",
                    run_id=run_id,
                    as_of_seq=1,
                    state={"key\x00字": "value\x00字"},
                )
                await store.save_projection_checkpoint(
                    checkpoint, expected_as_of_seq=None
                )
                assert (
                    await store.load_projection_checkpoint(
                        snapshot.key,
                        projection_name=checkpoint.projection_name,
                        run_id=run_id,
                        as_of_seq=1,
                    )
                    == checkpoint
                )
        finally:
            for writer in writers:
                await writer.aclose()


async def test_shared_tracer_queries_and_rebuilds_only_the_requested_namespace(
    trace_sql_engine: AsyncEngine,
) -> None:
    for store in (InMemoryTraceStore(), SqlAlchemyTraceStore(trace_sql_engine)):
        tracer = Tracer(store=store)
        alpha, beta = _identity(), _identity("beta")
        await _record(tracer, alpha)
        await _record(tracer, beta)
        for identity in (alpha, beta):
            history = await tracer.get(identity.thread)
            assert history.key.thread == identity.thread
            assert all(
                event.fact.identity.namespace == identity.namespace
                for event in (await history.events()).items
            )
            assert (await tracer.get(identity)).key == history.key
            await tracer.rebuild_graph(identity.thread)
            graph = await tracer.query(identity.thread)
            assert graph.snapshot.as_of_seq == history.as_of_seq
        alpha_view = await tracer.get(alpha.thread)
        await alpha_view.delete()
        assert (await tracer.get(beta.thread)).key.namespace == "beta"


@pytest.mark.parametrize("path", ("cached", "codec"))
async def test_reader_rejects_a_foreign_namespace_fact_with_valid_payload_digest(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    store = InMemoryTraceStore()
    writer = await store.open_writer(_identity())
    committed = (await writer.append((_fact(_identity()),)))[0]
    codec = CanonicalTracePayloadCodec()
    foreign = _fact(_identity("beta"))
    encoded = codec.encode_fact(foreign)
    original = store.backend.read_event_page

    async def corrupt(request: TraceEventPageRequest) -> StoredTraceEventPage:
        page = await original(request)
        record = page.events[0]
        if path == "cached":
            changed = replace(
                record, validated_event=committed.model_copy(update={"fact": foreign})
            )
        else:
            changed = StoredTraceEvent(
                event_id=record.event_id,
                trace_seq=record.trace_seq,
                run_id=record.run_id,
                fact_kind=record.fact_kind,
                occurred_at=record.occurred_at,
                canonical_payload=encoded.data,
                payload_digest=encoded.digest,
                persisted_bytes=record.persisted_bytes,
            )
        return replace(page, events=(changed,))

    try:
        with monkeypatch.context() as patch:
            patch.setattr(store.backend, "read_event_page", corrupt)
            with pytest.raises(TraceStoreProtocolError, match="conflict"):
                await store.read_events(writer.key, after_seq=0, as_of_seq=1, limit=1)
    finally:
        await writer.aclose()


@pytest.mark.parametrize("target", ("namespace", "thread", "writer", "checkpoint"))
async def test_reducer_rejects_contradictory_scope_before_any_storage_effect(
    target: str,
) -> None:
    store = InMemoryTraceStore()
    identity = _identity()
    writer = await store.open_writer(identity)
    try:
        state = await store.backend.load_ledger_state(
            TraceLedgerStateRequest(
                namespace=identity.namespace,
                thread_id=identity.thread_id,
                run_id=identity.run_id,
            )
        )
        change = TraceLedgerChange(
            kind="open_writer",
            namespace=identity.namespace,
            limits=store.limits,
            options=store.options,
            identity=identity,
            owner_token="new-owner",
        )
        if target == "namespace":
            change = replace(change, namespace="beta")
        elif target == "thread":
            change = replace(
                change, key=writer.key.model_copy(update={"thread_id": "another"})
            )
        elif target == "writer":
            assert state.target_writer is not None
            state = replace(
                state, target_writer=replace(state.target_writer, run_id="another")
            )
        else:
            change = replace(
                change,
                checkpoint=TraceProjectionCheckpoint(
                    key=writer.key.model_copy(update={"namespace": "beta"}),
                    projection_name="test",
                    as_of_seq=0,
                    state={},
                ),
            )
        with pytest.raises(TraceStoreProtocolError, match="identities conflict"):
            resolve_ledger_change(change, state)
        assert (await store.snapshot(identity.thread)).as_of_seq == 0
    finally:
        await writer.aclose()


@pytest.mark.parametrize("operation", ("get", "query", "rebuild_graph"))
async def test_tracer_rejects_a_substitute_store_snapshot_from_another_namespace(
    monkeypatch: pytest.MonkeyPatch,
    operation: Literal["get", "query", "rebuild_graph"],
) -> None:
    store = InMemoryTraceStore()
    tracer = Tracer(store=store)
    await _record(tracer, _identity())
    foreign = await store.snapshot(_identity().thread)

    async def wrong_scope(identity: ThreadIdentity) -> StoreThreadSnapshot:
        return foreign

    with monkeypatch.context() as patch:
        patch.setattr(store, "snapshot", wrong_scope)
        with pytest.raises(TraceStoreProtocolError):
            if operation == "get":
                await tracer.get(_identity("beta").thread)
            elif operation == "query":
                await tracer.query(_identity("beta").thread)
            else:
                await tracer.rebuild_graph(_identity("beta").thread)


async def test_tracer_closes_a_foreign_writer_before_hydrating_any_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryTraceStore()
    writer = await store.open_writer(_identity("beta"))

    async def wrong_writer(identity: RunIdentity) -> TraceWriter:
        return writer

    async def unexpected_read(key: object) -> StoreThreadSnapshot:
        raise AssertionError("foreign writer must not read history")

    source = RunSourceContext(
        identity=_identity(),
        runtime_profile="deepagents-v2",
        input_kind="ordinary",
        input={},
        config={},
    )
    with monkeypatch.context() as patch:
        patch.setattr(store, "open_writer", wrong_writer)
        patch.setattr(store, "snapshot_key", unexpected_read)
        with pytest.raises(TraceStoreProtocolError, match="another Run"):
            await Tracer(store=store).open_run(source)
    with pytest.raises(TraceThreadNotFound):
        await store.snapshot(_identity("beta").thread)


@pytest.mark.parametrize("boundary", ("writer", "snapshot"))
@pytest.mark.parametrize("failure_type", (OSError, TypeError, ValueError))
async def test_rejected_startup_preserves_writer_cleanup_failure_and_original_cause(
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    failure_type: type[Exception],
) -> None:
    store = InMemoryTraceStore()
    identity = _identity("beta" if boundary == "writer" else "alpha")
    writer = await store.open_writer(identity)
    snapshot = await store.snapshot(identity.thread)
    original_close = writer.aclose
    cleanup = failure_type("independent writer cleanup failure")
    cause = LookupError("original close cause")
    closed = False

    async def close_then_fail() -> None:
        nonlocal closed
        await original_close()
        closed = True
        raise cleanup from cause

    async def supplied_writer(identity: RunIdentity) -> TraceWriter:
        return writer

    async def foreign_snapshot(key: object) -> StoreThreadSnapshot:
        return snapshot.model_copy(
            update={"key": snapshot.key.model_copy(update={"namespace": "beta"})}
        )

    source = RunSourceContext(
        identity=_identity(),
        runtime_profile="deepagents-v2",
        input_kind="ordinary",
        input={},
        config={},
    )
    with monkeypatch.context() as patch:
        patch.setattr(store, "open_writer", supplied_writer)
        patch.setattr(writer, "aclose", close_then_fail)
        if boundary == "snapshot":
            patch.setattr(store, "snapshot_key", foreign_snapshot)
        with pytest.raises(TraceStoreProtocolError) as captured:
            await Tracer(store=store).open_run(source)
    assert closed
    assert captured.value.__cause__ is cleanup
    assert cleanup.__cause__ is cause
    assert cleanup.__context__ is not captured.value
    with pytest.raises(TraceThreadNotFound):
        await store.snapshot(identity.thread)


@pytest.mark.parametrize("phase", ("registration", "read"))
@pytest.mark.parametrize("failure_type", (OSError, TypeError, ValueError))
async def test_sql_provider_failures_preserve_scope_and_hide_vendor_details(
    trace_sql_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    failure_type: type[Exception],
) -> None:
    store = SqlAlchemyTraceStore(trace_sql_engine)
    beta = await store.open_writer(_identity("beta"))
    await beta.append((_fact(_identity("beta")),))
    await beta.aclose()
    alpha: TraceWriter | None = None
    if phase == "read":
        alpha = await store.open_writer(_identity())
        await alpha.append((_fact(_identity()),))
    execute = AsyncConnection.execute
    failure = failure_type("private-provider-detail")
    cause = LookupError("original provider cause")
    injected = False

    async def completed_command(
        connection: AsyncConnection, statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        nonlocal injected
        result = await execute(connection, statement, *args, **kwargs)
        sql = str(statement)
        target = (
            sql.startswith("INSERT INTO tinkerfin_trace_namespaces")
            if phase == "registration"
            else "FROM tinkerfin_trace_threads" in sql
        )
        if connection.engine is trace_sql_engine and target and not injected:
            injected = True
            raise failure from cause
        return result

    try:
        with monkeypatch.context() as patch:
            patch.setattr(AsyncConnection, "execute", completed_command)
            with pytest.raises(TraceStoreError) as captured:
                if phase == "registration":
                    await store.open_writer(_identity())
                else:
                    await store.snapshot(_identity().thread)
        assert injected
        assert "private-provider-detail" not in str(captured.value)
        assert captured.value.cause is failure
        assert failure.__cause__ is cause
        assert (await store.snapshot(_identity("beta").thread)).as_of_seq == 1
        if phase == "registration":
            with pytest.raises(TraceThreadNotFound):
                await store.snapshot(_identity().thread)
        else:
            assert (await store.snapshot(_identity().thread)).as_of_seq == 1
    finally:
        if alpha is not None:
            await alpha.aclose()
