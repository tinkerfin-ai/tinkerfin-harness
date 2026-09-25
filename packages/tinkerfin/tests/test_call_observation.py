"""Provider and Tool call observation through the managed Runtime facade."""

from __future__ import annotations

import asyncio
import gc
import weakref
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, cast
from uuid import uuid4

import pytest
from langchain.agents.middleware import ToolRetryMiddleware
from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)
from langchain.tools import tool
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import (
    FakeListChatModel,
    FakeMessagesListChatModel,
)
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_core.outputs import (
    ChatGeneration,
    ChatGenerationChunk,
    ChatResult,
    LLMResult,
)
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from tinkerfin import (
    RunIdentity,
    TinkerFin,
    trace_contribution,
)
from tinkerfin._observation import RuntimeObservationHub
from tinkerfin.runtime_profile import (
    DeepAgentsRuntimeProfile,
    DeepAgentsV2RuntimeProfile,
    DeepAgentsV3RuntimeProfile,
)
from tinkerfin_contracts import (
    ContextContributionObservation,
    ModelCallObservation,
    NativeMessageObservation,
    NativeTaskObservation,
    ObservationBoundary,
    RunObservationSession,
    RunSourceContext,
    RuntimeObservation,
    ToolExecutionObservation,
)
from tinkerfin_tracing import SubagentFact, TraceGraphNodeKind, Tracer


class _Session:
    def __init__(self) -> None:
        self.observations: list[RuntimeObservation] = []
        self.boundaries: list[ObservationBoundary] = []
        self.failure: asyncio.Future[BaseException] | None = None

    async def observe(self, observation: RuntimeObservation) -> None:
        self.observations.append(observation)

    async def force(self, boundary: ObservationBoundary) -> None:
        self.boundaries.append(boundary)

    def failure_waiter(self) -> Awaitable[BaseException]:
        if self.failure is None:
            self.failure = asyncio.get_running_loop().create_future()
        return self.failure

    async def aclose(self) -> None:
        return None


class _Observer:
    def __init__(self, session: _Session) -> None:
        self.session = session
        self.contexts: list[RunSourceContext] = []

    async def open_run(self, context: RunSourceContext) -> RunObservationSession:
        self.contexts.append(context)
        return self.session


class _StreamingModel(FakeListChatModel):
    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> Runnable:
        del tools, kwargs
        return self


class _MessageModel(FakeMessagesListChatModel):
    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> Runnable:
        del tools, kwargs
        return self


class _RewriteRequest(AgentMiddleware[Any, Any, Any]):
    @property
    def name(self) -> str:
        return "rewrite-request"

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        return await handler(
            request.override(
                messages=[HumanMessage(content="final-user", id="final-user-id")],
                system_message=SystemMessage(content="final-system"),
                model_settings={"temperature": 0.2},
            )
        )


class _ContributingMiddleware(AgentMiddleware[Any, Any, Any]):
    def __init__(self, name: str, calls: list[str]) -> None:
        self._name = name
        self.calls = calls

    @property
    def name(self) -> str:
        return self._name

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        self.calls.append(self.name)
        async with trace_contribution(
            kind="memory",
            name=f"{self.name} memory",
            input={"query": "safe"},
        ) as contribution:
            response = await handler(request)
            contribution.set_result({"matches": 1})
            return response


class _FailBeforeModel(AgentMiddleware[Any, Any, Any]):
    @property
    def name(self) -> str:
        return "guardrail"

    async def abefore_model(self, state: Any, runtime: Any) -> None:
        del state, runtime
        raise RuntimeError("blocked before model")


def _identity(run_id: str) -> RunIdentity:
    return RunIdentity(
        namespace="test", thread_id="thread-call-observation", run_id=run_id
    )


def test_error_claim_retains_identity_for_the_run_lifetime() -> None:
    class ClaimedError(RuntimeError):
        pass

    context = RunSourceContext(
        identity=_identity("run-error-identity"),
        runtime_profile="deepagents-v2",
        input_kind="ordinary",
        input={"messages": []},
        config={},
    )
    hub = RuntimeObservationHub(context=context, observers=())
    error = ClaimedError("first failure")
    reference = weakref.ref(error)

    assert hub.claim_error(error) is True
    assert hub.claim_error(error) is False
    del error
    gc.collect()

    assert reference() is not None
    assert hub.claim_error(ClaimedError("distinct failure")) is True


@pytest.mark.asyncio
async def test_parallel_identical_tasks_keep_exact_graph_execution_ownership() -> None:
    """Actual task identities distinguish simultaneous delegates with identical input."""

    session = _Session()
    tracer = Tracer()
    runtime = (
        TinkerFin(checkpointer=MemorySaver())
        .with_namespace("test")
        .with_observer(_Observer(session))
        .with_observer(tracer)
        .build(
            model=_MessageModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "task",
                                "id": call_id,
                                "args": {
                                    "description": "Analyze the same input",
                                    "subagent_type": "worker",
                                },
                            }
                            for call_id in ("delegate-one", "delegate-two")
                        ],
                    ),
                    AIMessage(content="Complete"),
                ]
            ),
            subagents=[
                {
                    "name": "worker",
                    "description": "Analyze inputs",
                    "system_prompt": "Analyze the assigned input",
                    "model": _MessageModel(responses=[AIMessage(content="Analyzed")]),
                    "tools": [],
                }
            ],
        )
    )
    stream = runtime.open_agui_run(
        thread_id="parallel-identical",
        run_id="run",
        messages=[{"id": "user", "role": "user", "content": "Analyze twice"}],
    )
    events = [event async for event in stream]
    assert stream.error is None
    assert events[-1].type.value == "RUN_FINISHED"

    native_task_ids: dict[str, str] = {}
    for observation in session.observations:
        if (
            isinstance(observation, NativeTaskObservation)
            and observation.phase == "start"
            and observation.name == "tools"
        ):
            assert isinstance(observation.input, list)
            assert len(observation.input) == 1
            call = observation.input[0]
            assert isinstance(call, dict)
            call_id = call.get("id")
            assert isinstance(call_id, str)
            native_task_ids[call_id] = observation.task_id
    executions = [
        observation
        for observation in session.observations
        if isinstance(observation, ToolExecutionObservation)
        and observation.tool_name == "task"
    ]
    assert len(native_task_ids) == len(set(native_task_ids.values())) == 2
    assert len(executions) == 4
    for execution in executions:
        assert execution.tool_call_id is not None
        assert execution.graph_task_id == native_task_ids[execution.tool_call_id]
    model_scopes = {
        observation.graph_namespace
        for observation in session.observations
        if isinstance(observation, ModelCallObservation) and observation.graph_namespace
    }
    assert model_scopes == {
        (f"tools:{task_id}",) for task_id in native_task_ids.values()
    }

    snapshot = await tracer.store.snapshot(
        runtime.thread_identity("parallel-identical")
    )
    trace_events = await tracer.store.read_events(
        snapshot.key, after_seq=0, as_of_seq=snapshot.as_of_seq, limit=100
    )
    subagents = [
        event.fact
        for event in trace_events
        if isinstance(event.fact, SubagentFact) and event.fact.phase == "started"
    ]
    assert {fact.parent_tool_call_id: fact.graph_namespace for fact in subagents} == {
        call_id: (f"tools:{task_id}",) for call_id, task_id in native_task_ids.items()
    }
    graph = await tracer.query(runtime.thread_identity("parallel-identical"))
    assert sum(node.kind is TraceGraphNodeKind.SUBAGENT for node in graph.nodes) == 2


@pytest.mark.asyncio
async def test_task_retry_keeps_logical_graph_identity_and_distinct_executions() -> (
    None
):
    """A repeated Tool attempt retains its request and records a fresh execution."""

    class FailOnceModel(_MessageModel):
        attempts: int = 0

        async def _agenerate(
            self,
            messages: list[BaseMessage],
            stop: list[str] | None = None,
            run_manager: AsyncCallbackManagerForLLMRun | None = None,
            **kwargs: Any,
        ) -> ChatResult:
            del messages, stop, run_manager, kwargs
            self.attempts += 1
            if self.attempts == 1:
                raise ValueError("child attempt failed")
            return ChatResult(generations=[ChatGeneration(message=self.responses[0])])

    child = FailOnceModel(responses=[AIMessage(content="Analyzed")])
    session = _Session()
    tracer = Tracer()
    runtime = (
        TinkerFin(checkpointer=MemorySaver())
        .with_namespace("test")
        .with_observer(_Observer(session))
        .with_observer(tracer)
        .build(
            model=_MessageModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "task",
                                "id": "delegate",
                                "args": {
                                    "description": "Analyze",
                                    "subagent_type": "worker",
                                },
                            }
                        ],
                    ),
                    AIMessage(content="Complete"),
                ]
            ),
            subagents=[
                {
                    "name": "worker",
                    "description": "Analyze inputs",
                    "system_prompt": "Analyze the assigned input",
                    "model": child,
                    "tools": [],
                }
            ],
            middleware=[
                ToolRetryMiddleware(
                    tools=["task"],
                    max_retries=1,
                    retry_on=(ValueError,),
                    initial_delay=0,
                    jitter=False,
                    on_failure="error",
                )
            ],
        )
    )
    stream = runtime.open_run(
        thread_id="task-retry",
        run_id="run",
        input={"messages": [HumanMessage(content="Analyze")]},
    )
    _parts = [part async for part in stream]
    assert stream.error is None
    assert child.attempts == 2
    executions = [
        observation
        for observation in session.observations
        if isinstance(observation, ToolExecutionObservation)
        and observation.tool_name == "task"
    ]
    assert [execution.phase for execution in executions] == [
        "started",
        "failed",
        "started",
        "completed",
    ]
    assert executions[0].execution_id == executions[1].execution_id
    assert executions[2].execution_id == executions[3].execution_id
    assert executions[0].execution_id != executions[2].execution_id
    assert executions[1].error_type == "builtins.ValueError"
    assert executions[1].error_message == "child attempt failed"
    task_id = executions[0].graph_task_id
    assert task_id is not None
    assert {execution.graph_task_id for execution in executions} == {task_id}
    assert {execution.tool_call_id for execution in executions} == {"delegate"}
    trace = await tracer.get(runtime.thread_identity("task-retry"))
    assert trace.status.execution == "succeeded"


@pytest.mark.asyncio
async def test_model_call_records_final_request_and_first_output_before_native() -> (
    None
):
    """The provider lifecycle does not wait for the Native message projection."""

    session = _Session()
    tinkerfin = TinkerFin().with_namespace("test").with_observer(_Observer(session))
    definition = tinkerfin.build(
        model=_StreamingModel(responses=["done"]),
        tools=[],
        middleware=[_RewriteRequest()],
    )
    stream = definition.open_run(
        thread_id=_identity("run-model").thread_id,
        run_id=_identity("run-model").run_id,
        input={"messages": [HumanMessage(content="original-user")]},
    )

    async for _part in stream:
        pass

    model_calls = [
        observation
        for observation in session.observations
        if isinstance(observation, ModelCallObservation)
    ]
    assert [observation.phase for observation in model_calls] == [
        "started",
        "first_output",
        "completed",
    ]
    assert tuple(
        (message.message_type, message.content, message.id)
        for message in model_calls[0].messages
    ) == (
        ("system", "final-system", None),
        ("human", "final-user", "final-user-id"),
    )
    first_output_id = model_calls[1].output_message_ids
    completed_ids = model_calls[2].output_message_ids
    assert len(first_output_id) == 1
    assert first_output_id == completed_ids
    native_assistant_ids = {
        observation.message.id
        for observation in session.observations
        if observation.kind == "native.message"
        and observation.message.message_type in {"assistant", "assistant_chunk"}
    }
    assert native_assistant_ids == set(completed_ids)
    first_model_index = session.observations.index(model_calls[0])
    first_native_index = next(
        index
        for index, observation in enumerate(session.observations)
        if observation.kind == "native.message"
    )
    assert first_model_index < first_native_index
    assert not any(
        observation.kind == "call.agent_step" for observation in session.observations
    )
    assert session.boundaries.count(ObservationBoundary.CALL_STARTED) == 1
    assert ObservationBoundary.TERMINAL in session.boundaries


@pytest.mark.asyncio
async def test_managed_ainvoke_records_the_same_runtime_observations() -> None:
    """Managed invoke keeps tracing while returning only the final root state."""

    session = _Session()
    tinkerfin = TinkerFin().with_namespace("test").with_observer(_Observer(session))
    definition = tinkerfin.build(model=_StreamingModel(responses=["done"]), tools=[])

    state = await definition.ainvoke(
        thread_id=_identity("run-managed-invoke").thread_id,
        run_id=_identity("run-managed-invoke").run_id,
        input={"messages": [HumanMessage(content="invoke", id="invoke-user")]},
    )

    messages = cast(Sequence[BaseMessage], state["messages"])
    assert messages[-1].content == "done"
    assert [
        observation.phase
        for observation in session.observations
        if isinstance(observation, ModelCallObservation)
    ] == ["started", "first_output", "completed"]
    assert session.observations[0].kind == "run.started"
    assert session.observations[-1].kind == "run.closed"


@pytest.mark.asyncio
async def test_v3_managed_ainvoke_preserves_model_and_native_message_identity() -> None:
    """The explicit v3 source feeds the same protocol-neutral observation boundary."""

    session = _Session()
    tinkerfin = (
        TinkerFin(runtime_profile=DeepAgentsV3RuntimeProfile())
        .with_namespace("test")
        .with_observer(_Observer(session))
    )
    definition = tinkerfin.build(model=_StreamingModel(responses=["done"]), tools=[])

    state = await definition.ainvoke(
        thread_id=_identity("run-managed-v3").thread_id,
        run_id=_identity("run-managed-v3").run_id,
        input={"messages": [HumanMessage(content="invoke", id="invoke-user-v3")]},
    )

    messages = cast(Sequence[BaseMessage], state["messages"])
    assert messages[-1].text == "done"
    model_calls = [
        observation
        for observation in session.observations
        if isinstance(observation, ModelCallObservation)
    ]
    assert [observation.phase for observation in model_calls] == [
        "started",
        "first_output",
        "completed",
    ]
    native_assistant_ids = {
        observation.message.id
        for observation in session.observations
        if observation.kind == "native.message"
        and observation.message.message_type in {"assistant", "assistant_chunk"}
    }
    assert native_assistant_ids == set(model_calls[-1].output_message_ids)
    assert {observation.kind for observation in session.observations}.issuperset(
        {"native.message", "native.state", "native.task"}
    )


@pytest.mark.asyncio
async def test_v3_restores_the_stable_tool_message_from_state() -> None:
    """The v3 messages projection omission must not remove Tool results."""

    @tool
    async def echo(value: str) -> str:
        """Return one value.

        Args:
            value: Value to return.

        Returns:
            The supplied value.
        """

        return value

    session = _Session()
    tinkerfin = (
        TinkerFin(runtime_profile=DeepAgentsV3RuntimeProfile())
        .with_namespace("test")
        .with_observer(_Observer(session))
    )
    definition = tinkerfin.build(
        model=_MessageModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "echo",
                            "args": {"value": "kept"},
                            "id": "call-v3-tool",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="done"),
            ]
        ),
        tools=[echo],
    )

    await definition.ainvoke(
        thread_id=_identity("run-v3-tool").thread_id,
        run_id=_identity("run-v3-tool").run_id,
        input={"messages": [HumanMessage(content="use the Tool")]},
    )

    tool_messages = [
        observation.message
        for observation in session.observations
        if isinstance(observation, NativeMessageObservation)
        and observation.message.message_type == "tool"
    ]
    assert len(tool_messages) == 1
    assert tool_messages[0].id is not None
    assert tool_messages[0].tool_call_id == "call-v3-tool"
    assert tool_messages[0].content == "kept"


@pytest.mark.asyncio
async def test_v3_preserves_subagent_namespaces_and_model_calls() -> None:
    """Nested work remains visible after v3 events enter the Native boundary."""

    session = _Session()
    tinkerfin = (
        TinkerFin(runtime_profile=DeepAgentsV3RuntimeProfile())
        .with_namespace("test")
        .with_observer(_Observer(session))
    )
    definition = tinkerfin.build(
        model=_MessageModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {
                                "description": "Complete the delegated work",
                                "subagent_type": "researcher",
                            },
                            "id": "call-v3-subagent",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="root done"),
            ]
        ),
        tools=[],
        subagents=[
            {
                "name": "researcher",
                "description": "Complete delegated work",
                "system_prompt": "Return the result.",
                "model": _MessageModel(responses=[AIMessage(content="child done")]),
                "tools": [],
            }
        ],
    )

    await definition.ainvoke(
        thread_id=_identity("run-v3-subagent").thread_id,
        run_id=_identity("run-v3-subagent").run_id,
        input={"messages": [HumanMessage(content="delegate")]},
    )

    assert any(
        observation.kind == "native.task" and observation.graph_namespace
        for observation in session.observations
    )
    assert any(
        isinstance(observation, ModelCallObservation)
        and observation.agent_name == "researcher"
        and observation.graph_namespace
        for observation in session.observations
    )
    assert any(
        isinstance(observation, NativeMessageObservation)
        and observation.graph_namespace
        and observation.message.message_type == "assistant"
        and observation.message.content == "child done"
        for observation in session.observations
    )


@pytest.mark.asyncio
async def test_model_output_ids_complete_when_the_first_chunk_has_no_identity() -> None:
    """Completion retains stable output IDs without guessing from chunk order."""

    session = _Session()
    context = RunSourceContext(
        identity=_identity("run-late-output-id"),
        runtime_profile="deepagents-v2",
        input_kind="ordinary",
        input={"messages": [{"role": "user", "content": "hello"}]},
        config={},
    )
    hub = RuntimeObservationHub(context=context, observers=(_Observer(session),))
    await hub.start()
    handler = hub.call_handler
    provider_run_id = uuid4()
    await handler.on_chat_model_start(
        {},
        [[HumanMessage(content="hello")]],
        run_id=provider_run_id,
    )
    await handler.on_llm_new_token(
        "a",
        chunk=ChatGenerationChunk(message=AIMessageChunk(content="a")),
        run_id=provider_run_id,
    )
    await handler.on_llm_end(
        LLMResult(
            generations=[
                [
                    ChatGeneration(message=AIMessage(content="first", id="output-1")),
                    ChatGeneration(message=AIMessage(content="second", id="output-2")),
                ],
                [ChatGeneration(message=AIMessage(content="repeat", id="output-1"))],
            ]
        ),
        run_id=provider_run_id,
    )
    await hub.terminal("succeeded")
    await hub.close()

    model_calls = [
        observation
        for observation in session.observations
        if isinstance(observation, ModelCallObservation)
    ]
    assert model_calls[1].phase == "first_output"
    assert model_calls[1].output_message_ids == ()
    assert model_calls[2].phase == "completed"
    assert model_calls[2].output_message_ids == ("output-1", "output-2")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runtime_profile",
    [DeepAgentsV2RuntimeProfile(), DeepAgentsV3RuntimeProfile()],
    ids=["v2-astream", "v3-astream-events"],
)
async def test_before_model_failure_does_not_create_middleware_observations(
    runtime_profile: DeepAgentsRuntimeProfile,
) -> None:
    session = _Session()
    tinkerfin = (
        TinkerFin(runtime_profile=runtime_profile)
        .with_namespace("test")
        .with_observer(_Observer(session))
    )
    definition = tinkerfin.build(
        model=_StreamingModel(responses=["unused"]),
        tools=[],
        middleware=[_FailBeforeModel()],
    )
    stream = definition.open_run(
        thread_id=_identity("run-middleware-error").thread_id,
        run_id=_identity("run-middleware-error").run_id,
        input={"messages": [HumanMessage(content="check guardrail")]},
    )

    with pytest.raises(RuntimeError, match="blocked before model"):
        async for _part in stream:
            pass

    assert not any(
        isinstance(observation, ModelCallObservation)
        for observation in session.observations
    )
    assert not any(
        observation.kind == "call.agent_step" for observation in session.observations
    )
    assert any(
        observation.kind == "run.terminal" and observation.outcome == "failed"
        for observation in session.observations
    )


@pytest.mark.asyncio
async def test_tool_execution_records_the_actual_input_and_result() -> None:
    """Tool execution remains distinct from the preceding model proposal."""

    @tool
    async def add_values(a: int, b: int) -> str:
        """Add two values.

        Args:
            a: First value.
            b: Second value.

        Returns:
            Decimal sum.
        """

        return str(a + b)

    model = _MessageModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "add_values",
                        "args": {"a": 2, "b": 3},
                        "id": "call-add",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    session = _Session()
    tinkerfin = TinkerFin().with_namespace("test").with_observer(_Observer(session))
    definition = tinkerfin.build(model=model, tools=[add_values])
    stream = definition.open_run(
        thread_id=_identity("run-tool").thread_id,
        run_id=_identity("run-tool").run_id,
        input={"messages": [HumanMessage(content="add")]},
    )

    async for _part in stream:
        pass

    executions = [
        observation
        for observation in session.observations
        if isinstance(observation, ToolExecutionObservation)
    ]
    assert [observation.phase for observation in executions] == [
        "started",
        "completed",
    ]
    assert executions[0].tool_call_id == "call-add"
    assert executions[0].input == {"a": 2, "b": 3}
    assert isinstance(executions[1].output, dict)
    assert executions[1].output["$type"] == "langchain.message"
    output_value = executions[1].output["value"]
    assert isinstance(output_value, dict)
    output_data = output_value["data"]
    assert isinstance(output_data, dict)
    assert output_data["content"] == "5"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runtime_profile",
    [DeepAgentsV2RuntimeProfile(), DeepAgentsV3RuntimeProfile()],
    ids=["v2-astream", "v3-astream-events"],
)
async def test_runtime_cancellation_closes_an_unmatched_tool_execution(
    runtime_profile: DeepAgentsRuntimeProfile,
) -> None:
    """Run settlement supplies the terminal callback that BaseTool omits."""

    entered = asyncio.Event()
    release = asyncio.Event()

    @tool
    async def wait_until_cancelled() -> str:
        """Wait for cancellation.

        Returns:
            An unreachable value.
        """

        entered.set()
        await release.wait()
        return "unreachable"

    model = _MessageModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "wait_until_cancelled",
                        "args": {},
                        "id": "call-cancel",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    )
    session = _Session()
    tinkerfin = (
        TinkerFin(runtime_profile=runtime_profile)
        .with_namespace("test")
        .with_observer(_Observer(session))
    )
    definition = tinkerfin.build(model=model, tools=[wait_until_cancelled])
    stream = definition.open_run(
        thread_id=_identity("run-cancel").thread_id,
        run_id=_identity("run-cancel").run_id,
        input={"messages": [HumanMessage(content="wait")]},
    )

    async def consume() -> None:
        async for _part in stream:
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    executions = [
        observation
        for observation in session.observations
        if isinstance(observation, ToolExecutionObservation)
    ]
    assert [observation.phase for observation in executions] == [
        "started",
        "cancelled",
    ]
    assert executions[-1].error_type is None
    assert executions[-1].error_message is None
    terminal_index = next(
        index
        for index, observation in enumerate(session.observations)
        if observation.kind == "run.terminal"
    )
    assert session.observations.index(executions[-1]) < terminal_index
    assert not any(
        observation.kind == "call.agent_step" for observation in session.observations
    )


@pytest.mark.asyncio
async def test_middleware_execution_is_preserved_without_trace_metadata() -> None:
    """Middleware keeps its behavior without entering observation metadata."""

    calls: list[str] = []
    metrics = _ContributingMiddleware("internal-metrics", calls)
    visible = _ContributingMiddleware("customer-memory", calls)
    session = _Session()
    observer = _Observer(session)
    tinkerfin = TinkerFin().with_namespace("test").with_observer(observer)
    definition = tinkerfin.build(
        model=_StreamingModel(responses=["done"]),
        tools=[],
        middleware=[metrics, visible],
    )
    stream = definition.open_run(
        thread_id=_identity("run-middleware").thread_id,
        run_id=_identity("run-middleware").run_id,
        input={"messages": [HumanMessage(content="remember")]},
    )

    async for _part in stream:
        pass

    assert calls == ["internal-metrics", "customer-memory"]
    assert len(observer.contexts) == 1
    assert not hasattr(observer.contexts[0], "middleware")
    contributions = [
        observation
        for observation in session.observations
        if isinstance(observation, ContextContributionObservation)
    ]
    assert [item.phase for item in contributions] == [
        "started",
        "started",
        "completed",
        "completed",
    ]
    assert {item.name for item in contributions} == {
        "internal-metrics memory",
        "customer-memory memory",
    }
    assert contributions[-1].output == {"matches": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runtime_profile",
    [DeepAgentsV2RuntimeProfile(), DeepAgentsV3RuntimeProfile()],
    ids=["v2-astream", "v3-astream-events"],
)
async def test_rejected_tool_review_never_records_an_execution(
    runtime_profile: DeepAgentsRuntimeProfile,
) -> None:
    @tool
    async def protected_action(value: str) -> str:
        """Return one reviewed value.

        Args:
            value: Reviewed input.

        Returns:
            The accepted value.
        """

        return value

    model = _MessageModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "protected_action",
                        "args": {"value": "original"},
                        "id": "call-reviewed-reject",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    session = _Session()
    tinkerfin = (
        TinkerFin(checkpointer=MemorySaver(), runtime_profile=runtime_profile)
        .with_namespace("test")
        .with_observer(_Observer(session))
    )
    definition = tinkerfin.build(
        model=model,
        tools=[protected_action],
        interrupt_on={
            "protected_action": {"allowed_decisions": ["approve", "edit", "reject"]}
        },
    )
    first = definition.open_run(
        thread_id=_identity("run-review-reject-start").thread_id,
        run_id=_identity("run-review-reject-start").run_id,
        input={"messages": [HumanMessage(content="review")]},
    )
    async for _part in first:
        pass
    interrupted_observations = tuple(session.observations)
    assert not any(
        observation.kind == "call.agent_step"
        for observation in interrupted_observations
    )
    assert any(
        observation.kind == "run.terminal" and observation.outcome == "interrupted"
        for observation in interrupted_observations
    )
    resumed = definition.open_run(
        thread_id=_identity("run-review-reject-resume").thread_id,
        run_id=_identity("run-review-reject-resume").run_id,
        input=Command(
            resume={"decisions": [{"type": "reject", "message": "not allowed"}]}
        ),
    )
    async for _part in resumed:
        pass

    assert not any(
        isinstance(observation, ToolExecutionObservation)
        for observation in session.observations
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runtime_profile",
    [DeepAgentsV2RuntimeProfile(), DeepAgentsV3RuntimeProfile()],
    ids=["v2-astream", "v3-astream-events"],
)
async def test_edited_tool_review_records_only_the_actual_input(
    runtime_profile: DeepAgentsRuntimeProfile,
) -> None:
    @tool
    async def add_reviewed(a: int, b: int) -> str:
        """Add reviewed values.

        Args:
            a: First reviewed value.
            b: Second reviewed value.

        Returns:
            Decimal sum.
        """

        return str(a + b)

    model = _MessageModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "add_reviewed",
                        "args": {"a": 1, "b": 2},
                        "id": "call-reviewed-edit",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    session = _Session()
    tinkerfin = (
        TinkerFin(checkpointer=MemorySaver(), runtime_profile=runtime_profile)
        .with_namespace("test")
        .with_observer(_Observer(session))
    )
    definition = tinkerfin.build(
        model=model,
        tools=[add_reviewed],
        interrupt_on={
            "add_reviewed": {"allowed_decisions": ["approve", "edit", "reject"]}
        },
    )
    first = definition.open_run(
        thread_id=_identity("run-review-edit-start").thread_id,
        run_id=_identity("run-review-edit-start").run_id,
        input={"messages": [HumanMessage(content="review")]},
    )
    async for _part in first:
        pass
    resumed = definition.open_run(
        thread_id=_identity("run-review-edit-resume").thread_id,
        run_id=_identity("run-review-edit-resume").run_id,
        input=Command(
            resume={
                "decisions": [
                    {
                        "type": "edit",
                        "edited_action": {
                            "name": "add_reviewed",
                            "args": {"a": 7, "b": 8},
                        },
                    }
                ]
            }
        ),
    )
    async for _part in resumed:
        pass

    executions = [
        observation
        for observation in session.observations
        if isinstance(observation, ToolExecutionObservation)
    ]
    assert [observation.phase for observation in executions] == [
        "started",
        "completed",
    ]
    assert executions[0].input == {"a": 7, "b": 8}
