"""Public business Redactor, mandatory safety, and fail-closed Trace contracts."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tinkerfin_contracts import (
    ModelCallObservation,
    NativeInterruptRecord,
    NativeMessageRecord,
    NativeReasoningObservation,
    NativeStateObservation,
    NativeToolCall,
    RunClosedObservation,
    RunIdentity,
    RunInputKind,
    RunInputObservation,
    RunObservationSession,
    RunSourceContext,
    RunStartedObservation,
    RunTerminalObservation,
    ToolExecutionObservation,
)
from tinkerfin_tracing import (
    CanonicalTracePayloadCodec,
    CompositeRedactor,
    EncodedTracePayload,
    ReasoningCapturePolicy,
    RedactionContext,
    SqlAlchemyTraceStore,
    TraceCaptureRejected,
    Tracer,
    TraceSemanticFact,
    redact_json_paths,
)
from tinkerfin_tracing.facts import ModelCallFact, ToolExecutionFact
from tinkerfin_tracing.graph import TraceGraphFilter, TraceGraphNodeKind


class _InspectingCodec(CanonicalTracePayloadCodec):
    def __init__(self) -> None:
        self.encoded_facts: list[str] = []

    def encode_fact(self, fact: TraceSemanticFact) -> EncodedTracePayload:
        encoded = fact.model_dump_json(by_alias=True)
        self.encoded_facts.append(encoded)
        return super().encode_fact(fact)


def _stamp(index: int) -> tuple[datetime, int]:
    return datetime.now(UTC), index


def _context(
    run_id: str,
    *,
    input_kind: RunInputKind = "ordinary",
    parent_run_id: str | None = None,
    content: JsonValue = "hello",
) -> RunSourceContext:
    return RunSourceContext(
        identity=RunIdentity(
            namespace="test", thread_id="thread-redaction", run_id=run_id
        ),
        runtime_profile="deepagents-v2",
        input_kind=input_kind,
        parent_run_id=parent_run_id,
        input={
            "messages": [
                {
                    "role": "user",
                    "id": f"human-{run_id}",
                    "content": content,
                }
            ]
        },
        config={"configurable": {"thread_id": "thread-redaction"}},
        call_tracking_enabled=True,
    )


async def _start(
    tracer: Tracer,
    context: RunSourceContext,
) -> RunObservationSession:
    session = await tracer.open_run(context)
    observed_at, monotonic_ns = _stamp(1)
    await session.observe(
        RunStartedObservation(
            identity=context.identity,
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
        )
    )
    observed_at, monotonic_ns = _stamp(2)
    await session.observe(
        RunInputObservation(
            identity=context.identity,
            source=context,
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
        )
    )
    return session


async def _finish(
    session: RunObservationSession,
    context: RunSourceContext,
) -> None:
    observed_at, monotonic_ns = _stamp(90)
    await session.observe(
        RunTerminalObservation(
            identity=context.identity,
            outcome="succeeded",
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
        )
    )
    observed_at, monotonic_ns = _stamp(91)
    await session.observe(
        RunClosedObservation(
            identity=context.identity,
            outcome="succeeded",
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
        )
    )
    await session.aclose()


class _BusinessRedactor:
    def __init__(self) -> None:
        self.contexts: list[RedactionContext] = []

    def redact(
        self,
        value: JsonValue,
        *,
        context: RedactionContext,
    ) -> JsonValue:
        self.contexts.append(context)
        copied = json.loads(json.dumps(value, ensure_ascii=False))
        if (
            context.content_kind == "tool_arguments"
            and context.component_name == "create_customer"
        ):
            copied = redact_json_paths(
                copied,
                paths=("/id_card_number", "/mobile", "/bank_account"),
            )
        if isinstance(copied, dict):
            copied["OPENAI_API_KEY"] = "reintroduced-credential"
            metadata: dict[str, JsonValue] = {
                "reasoning_content": "reintroduced-private-reasoning",
                "ordinary": "kept",
            }
            copied["additional_kwargs"] = metadata
        return copied


async def test_framework_safety_and_business_redaction_cover_model_tool_and_state() -> (
    None
):
    redactor = _BusinessRedactor()
    tracer = Tracer(redactor=redactor)
    context = _context(
        "run-redaction",
        content={
            "password": "input-credential",
            "reasoning_content": "business-reasoning",
            "additional_kwargs": {
                "reasoning_content": "private-reasoning",
                "ordinary": "kept",
            },
        },
    )
    session = await _start(tracer, context)
    observed_at, monotonic_ns = _stamp(3)
    await session.observe(
        ModelCallObservation(
            identity=context.identity,
            phase="started",
            call_id="model-redaction",
            provider="deepseek",
            model="deepseek-chat",
            messages=(
                NativeMessageRecord(
                    message_type="system",
                    content="system",
                ),
                NativeMessageRecord(
                    message_type="human",
                    content="customer",
                ),
            ),
            invocation={"api_key": "model-credential"},
            options={"temperature": 0},
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
        )
    )
    observed_at, monotonic_ns = _stamp(4)
    await session.observe(
        ModelCallObservation(
            identity=context.identity,
            phase="completed",
            call_id="model-redaction",
            provider="deepseek",
            usage={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
            response_metadata={"finish_reason": "stop"},
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
        )
    )
    observed_at, monotonic_ns = _stamp(5)
    await session.observe(
        ToolExecutionObservation(
            identity=context.identity,
            phase="started",
            execution_id="execution-redaction",
            tool_call_id="tool-redaction",
            tool_call_namespace=(),
            tool_name="create_customer",
            input={
                "id_card_number": "310000000000000000",
                "mobile": "13800000000",
                "bank_account": "6222000000000000",
                "name": "Customer",
            },
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
        )
    )
    observed_at, monotonic_ns = _stamp(6)
    await session.observe(
        ToolExecutionObservation(
            identity=context.identity,
            phase="completed",
            execution_id="execution-redaction",
            tool_call_id="tool-redaction",
            tool_call_namespace=(),
            tool_name="create_customer",
            output={"customer_id": "customer-1", "auth_token": "tool-credential"},
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
        )
    )
    observed_at, monotonic_ns = _stamp(7)
    await session.observe(
        NativeStateObservation(
            identity=context.identity,
            graph_namespace=(),
            state={
                "customer": {"password": "state-credential"},
                "reasoning_content": "business-state-value",
                "nested": {
                    "additional_kwargs": {
                        "reasoning_content": "private-state-reasoning",
                        "ordinary": "kept",
                    }
                },
                "tinkerfin_plan": {
                    "revision": 1,
                    "status": "draft",
                    "secret": "plan-credential",
                },
            },
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
        )
    )
    await _finish(session, context)

    thread = await tracer.get(context.identity.thread)
    events = (await thread.events(limit=200)).items
    encoded = "\n".join(event.model_dump_json(by_alias=True) for event in events)
    assert "input-credential" not in encoded
    assert "model-credential" not in encoded
    assert "tool-credential" not in encoded
    assert "state-credential" not in encoded
    assert "plan-credential" not in encoded
    assert "reintroduced-credential" not in encoded
    assert "private-reasoning" not in encoded
    assert "private-state-reasoning" not in encoded
    assert "reintroduced-private-reasoning" not in encoded
    assert "business-reasoning" in encoded
    assert "business-state-value" in encoded

    facts = tuple(event.fact for event in events)
    model = next(
        fact
        for fact in facts
        if isinstance(fact, ModelCallFact) and fact.phase == "started"
    )
    assert model.request is not None
    assert isinstance(model.request.value, dict)
    assert model.request.value["OPENAI_API_KEY"] == {"$type": "redacted"}
    assert model.request.value["additional_kwargs"] == {"ordinary": "kept"}
    tool = next(
        fact
        for fact in facts
        if isinstance(fact, ToolExecutionFact) and fact.phase == "started"
    )
    assert tool.input is not None
    assert isinstance(tool.input.value, dict)
    assert tool.input.value["name"] == "Customer"
    assert tool.input.value["mobile"] == {"$type": "redacted"}
    assert {item.content_kind for item in redactor.contexts}.issuperset(
        {
            "message",
            "model_request",
            "model_response",
            "tool_arguments",
            "tool_result",
            "state",
            "plan",
            "custom",
        }
    )
    assert all(not hasattr(item, "thread_id") for item in redactor.contexts)
    assert all(not hasattr(item, "run_id") for item in redactor.contexts)
    public_search = await tracer.query(
        context.identity.thread,
        where=TraceGraphFilter(
            kinds={TraceGraphNodeKind.HUMAN_MESSAGE},
            search="business-reasoning",
        ),
    )
    private_search = await tracer.query(
        context.identity.thread,
        where=TraceGraphFilter(
            search="private-reasoning",
        ),
    )
    assert len(public_search.matched_node_ids) == 1
    assert private_search.nodes == ()


class _MutatingRedactor:
    def redact(
        self,
        value: JsonValue,
        *,
        context: RedactionContext,
    ) -> JsonValue:
        del context
        assert isinstance(value, dict)
        value["changed"] = True
        return value


class _OpaqueRedactor:
    def redact(
        self,
        value: JsonValue,
        *,
        context: RedactionContext,
    ) -> JsonValue:
        del value, context
        return object()  # pyright: ignore[reportReturnType]


class _RaisingRedactor:
    def redact(
        self,
        value: JsonValue,
        *,
        context: RedactionContext,
    ) -> JsonValue:
        del value, context
        raise RuntimeError("raw-secret-must-not-be-public")


class _AwaitableRedactor:
    def redact(
        self,
        value: JsonValue,
        *,
        context: RedactionContext,
    ) -> JsonValue:
        del context

        async def result() -> JsonValue:
            return value

        return result()  # pyright: ignore[reportReturnType]


@pytest.mark.parametrize(
    "redactor, message",
    [
        (_MutatingRedactor(), "mutated its input"),
        (_OpaqueRedactor(), "returned invalid JSON"),
        (_RaisingRedactor(), "business redaction failed"),
        (_AwaitableRedactor(), "returned an awaitable"),
    ],
)
def test_composite_redactor_rejects_extension_contract_violations(
    redactor: object,
    message: str,
) -> None:
    composite = CompositeRedactor(redactor)  # pyright: ignore[reportArgumentType]
    with pytest.raises(TraceCaptureRejected, match=message):
        composite.redact(
            {"value": "safe"},
            context=RedactionContext(content_kind="custom"),
        )


class _AsyncRedactor:
    async def redact(
        self,
        value: JsonValue,
        *,
        context: RedactionContext,
    ) -> JsonValue:
        del context
        return value


def test_tracer_rejects_an_async_redactor_before_opening_store_resources() -> None:
    with pytest.raises(TypeError, match="must be synchronous"):
        Tracer(redactor=_AsyncRedactor())  # pyright: ignore[reportArgumentType]


class _ShapeBreakingRedactor:
    def __init__(self, target: str) -> None:
        self.target = target

    def redact(
        self,
        value: JsonValue,
        *,
        context: RedactionContext,
    ) -> JsonValue:
        if context.content_kind != self.target:
            return json.loads(json.dumps(value))
        if self.target == "model_request":
            assert isinstance(value, dict)
            return {key: item for key, item in value.items() if key != "messages"}
        if self.target == "state":
            return "removed"
        if self.target == "interaction":
            assert isinstance(value, dict)
            copied = json.loads(json.dumps(value))
            copied["review_configs"] = []
            return copied
        return value


async def test_model_request_and_state_required_shapes_fail_closed() -> None:
    for target in ("model_request", "state"):
        tracer = Tracer(redactor=_ShapeBreakingRedactor(target))
        context = _context(f"run-shape-{target}")
        session = await _start(tracer, context)
        observed_at, monotonic_ns = _stamp(3)
        if target == "model_request":
            observation: Any = ModelCallObservation(
                identity=context.identity,
                phase="started",
                call_id="model-shape",
                model="model-shape",
                messages=(NativeMessageRecord(message_type="human", content="hi"),),
                observed_at=observed_at,
                monotonic_ns=monotonic_ns,
            )
        else:
            observation = NativeStateObservation(
                identity=context.identity,
                graph_namespace=(),
                state={"value": "kept"},
                observed_at=observed_at,
                monotonic_ns=monotonic_ns,
            )
        with pytest.raises(TraceCaptureRejected):
            await session.observe(observation)
        await session.aclose()


async def test_hitl_review_semantics_cannot_be_removed_by_business_redaction() -> None:
    tracer = Tracer(redactor=_ShapeBreakingRedactor("interaction"))
    context = _context("run-hitl-shape")
    session = await _start(tracer, context)
    observed_at, monotonic_ns = _stamp(3)
    with pytest.raises(TraceCaptureRejected, match="review configuration order"):
        await session.observe(
            NativeStateObservation(
                identity=context.identity,
                graph_namespace=(),
                state={},
                messages=(
                    NativeMessageRecord(
                        message_type="assistant",
                        id="assistant-hitl",
                        content="",
                        tool_calls=(
                            NativeToolCall(
                                id="tool-hitl",
                                name="create_customer",
                                arguments={"mobile": "13800000000"},
                            ),
                        ),
                    ),
                ),
                interrupts=(
                    NativeInterruptRecord(
                        id="interrupt-hitl",
                        value={
                            "action_requests": [
                                {
                                    "name": "create_customer",
                                    "args": {"mobile": "13800000000"},
                                    "description": "Create customer",
                                }
                            ],
                            "review_configs": [
                                {
                                    "action_name": "create_customer",
                                    "allowed_decisions": ["approve", "reject"],
                                }
                            ],
                        },
                    ),
                ),
                observed_at=observed_at,
                monotonic_ns=monotonic_ns,
            )
        )
    await session.aclose()


class _CountingRedactor:
    def __init__(self) -> None:
        self.calls = 0

    def redact(
        self,
        value: JsonValue,
        *,
        context: RedactionContext,
    ) -> JsonValue:
        del context
        self.calls += 1
        return json.loads(json.dumps(value))


async def test_lineage_hydration_does_not_redact_persisted_values_again() -> None:
    redactor = _CountingRedactor()
    tracer = Tracer(redactor=redactor)
    root = _context("run-hydration-root")
    root_session = await _start(tracer, root)
    await _finish(root_session, root)
    calls_before_hydration = redactor.calls

    resumed = _context(
        "run-hydration-resume",
        input_kind="resume",
        parent_run_id=root.identity.run_id,
    )
    resumed_session = await tracer.open_run(resumed)
    assert redactor.calls == calls_before_hydration
    await resumed_session.aclose()


async def test_authorized_reasoning_uses_the_model_response_context() -> None:
    contexts: list[RedactionContext] = []

    class ReasoningRedactor:
        def redact(
            self,
            value: JsonValue,
            *,
            context: RedactionContext,
        ) -> JsonValue:
            contexts.append(context)
            if context.content_kind == "model_response" and value == "private thought":
                return "business-redacted thought"
            return json.loads(json.dumps(value))

    tracer = Tracer(
        redactor=ReasoningRedactor(),
        reasoning_capture_policy=ReasoningCapturePolicy.content(),
    )
    context = _context("run-reasoning-redaction")
    session = await _start(tracer, context)
    observed_at, monotonic_ns = _stamp(3)
    await session.observe(
        ModelCallObservation(
            identity=context.identity,
            phase="started",
            call_id="model-reasoning",
            model="deepseek-chat",
            messages=(NativeMessageRecord(message_type="human", content="think"),),
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
        )
    )
    observed_at, monotonic_ns = _stamp(4)
    await session.observe(
        ModelCallObservation(
            identity=context.identity,
            phase="first_output",
            call_id="model-reasoning",
            model="deepseek-chat",
            output_message_ids=("assistant-reasoning",),
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
        )
    )
    observed_at, monotonic_ns = _stamp(5)
    await session.observe(
        NativeReasoningObservation(
            identity=context.identity,
            graph_namespace=(),
            message_id="assistant-reasoning",
            extractor="deepseek.additional_kwargs.reasoning_content",
            content="private thought",
            snapshot=True,
            observed_at=observed_at,
            monotonic_ns=monotonic_ns,
        )
    )
    await _finish(session, context)

    trace = await tracer.get(context.identity.thread)
    assert trace.reasoning[0].content == "business-redacted thought"
    assert (
        RedactionContext(
            content_kind="model_response",
            component_name="deepseek-chat",
        )
        in contexts
    )


def test_redact_json_paths_is_immutable_and_supports_rfc_6901() -> None:
    source: JsonValue = {
        "id/card": "private",
        "nested": {"mobile~number": "13800000000"},
        "items": [{"bank": "6222"}],
    }
    redacted = redact_json_paths(
        source,
        paths=("/id~1card", "/nested/mobile~0number", "/items/0/bank"),
    )

    assert source == {
        "id/card": "private",
        "nested": {"mobile~number": "13800000000"},
        "items": [{"bank": "6222"}],
    }
    assert redacted == {
        "id/card": {"$type": "redacted"},
        "nested": {"mobile~number": {"$type": "redacted"}},
        "items": [{"bank": {"$type": "redacted"}}],
    }


def test_redaction_context_validates_only_functional_source_metadata() -> None:
    calls: list[RedactionContext] = []

    class ContextRecordingRedactor:
        def redact(
            self,
            value: JsonValue,
            *,
            context: RedactionContext,
        ) -> JsonValue:
            calls.append(context)
            return json.loads(json.dumps(value))

    context = RedactionContext(
        content_kind="tool_arguments",
        component_name="search",
    )
    CompositeRedactor(ContextRecordingRedactor()).redact(
        {"query": "safe"},
        context=context,
    )
    assert context.content_kind == "tool_arguments"
    assert context.component_name == "search"
    assert calls == [context]
    with pytest.raises(ValueError, match="component_name"):
        RedactionContext(content_kind="custom", component_name=" run ")


async def test_codec_receives_only_the_redacted_fact_graph(tmp_path: Path) -> None:
    codec = _InspectingCodec()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'redaction.db'}")
    store = SqlAlchemyTraceStore(engine, codec=codec)

    class MessageRedactor:
        def redact(
            self,
            value: JsonValue,
            *,
            context: RedactionContext,
        ) -> JsonValue:
            if context.content_kind == "message":
                return redact_json_paths(value, paths=("/mobile",))
            return json.loads(json.dumps(value))

    tracer = Tracer(store=store, redactor=MessageRedactor())
    context = _context(
        "run-codec-redaction",
        content={
            "mobile": "13800000000",
            "api_key": "codec-credential",
        },
    )
    try:
        session = await _start(tracer, context)
        await _finish(session, context)
        assert codec.encoded_facts
        encoded = "\n".join(codec.encoded_facts)
        assert "13800000000" not in encoded
        assert "codec-credential" not in encoded
        assert '"$type":"redacted"' in encoded
        safe_search = await tracer.query(
            context.identity.thread,
            where=TraceGraphFilter(
                kinds={TraceGraphNodeKind.HUMAN_MESSAGE},
                search="redacted",
            ),
        )
        pii_search = await tracer.query(
            context.identity.thread,
            where=TraceGraphFilter(
                kinds={TraceGraphNodeKind.HUMAN_MESSAGE},
                search="13800000000",
            ),
        )
        credential_search = await tracer.query(
            context.identity.thread,
            where=TraceGraphFilter(
                kinds={TraceGraphNodeKind.HUMAN_MESSAGE},
                search="codec-credential",
            ),
        )
        assert len(safe_search.matched_node_ids) == 1
        assert pii_search.nodes == ()
        assert credential_search.nodes == ()
    finally:
        await engine.dispose()


@pytest.mark.docker_integration
async def test_mysql_stores_only_the_final_redacted_payload(
    trace_mysql_url: str,
) -> None:
    engine = create_async_engine(trace_mysql_url)
    store = SqlAlchemyTraceStore(engine)

    class MessageRedactor:
        def redact(
            self,
            value: JsonValue,
            *,
            context: RedactionContext,
        ) -> JsonValue:
            if context.content_kind == "message":
                return redact_json_paths(value, paths=("/mobile",))
            return json.loads(json.dumps(value))

    tracer = Tracer(store=store, redactor=MessageRedactor())
    context = _context(
        "run-mysql-redaction",
        content={
            "mobile": "13900000000",
            "api_key": "mysql-credential",
        },
    )
    try:
        session = await _start(tracer, context)
        await _finish(session, context)
        async with engine.connect() as connection:
            payloads = (
                await connection.execute(
                    text(
                        "SELECT payload FROM tinkerfin_trace_events "
                        "WHERE namespace_hash = :namespace_hash"
                    ),
                    {
                        "namespace_hash": hashlib.sha256(
                            context.identity.namespace.encode("utf-8")
                        ).digest()
                    },
                )
            ).scalars()
        encoded = b"\n".join(bytes(payload) for payload in payloads)
        assert b"13900000000" not in encoded
        assert b"mysql-credential" not in encoded
        assert b"redacted" in encoded
    finally:
        await engine.dispose()
