"""Positive and negative inference contracts for the public Runtime API."""

from typing import TYPE_CHECKING, TypedDict, assert_type

from tinkerfin import AgentRuntime, AgUiRunStream, NativeRunStream, TinkerFin
from tinkerfin.agui_resume import AgUiResumeRequest
from tinkerfin.runtime import TinkerFin as RuntimeBuilder
from tinkerfin_contracts import RunTerminalObservation


class _Context(TypedDict):
    customer: str


async def terminal(terminal: RunTerminalObservation) -> None:
    assert_type(terminal.identity.namespace, str)


if TYPE_CHECKING:
    source_runtime = (
        RuntimeBuilder()
        .with_namespace("chosen")
        .build(model="provider:model", context_schema=_Context)
    )
    assert_type(source_runtime, AgentRuntime[_Context])
    source_runtime.open_run(
        thread_id="thread",
        run_id="run",
        input={"messages": []},
        context="wrong",  # pyright: ignore[reportArgumentType]
    )
    builder = TinkerFin().with_namespace("chosen").with_observer(on_terminal=terminal)
    assert_type(builder, TinkerFin)
    runtime = builder.build(model="provider:model", context_schema=_Context)
    assert_type(runtime, AgentRuntime[_Context])
    ordinary = builder.build(model="provider:model")
    assert_type(ordinary, AgentRuntime[None])
    native = runtime.open_run(
        thread_id="thread",
        run_id="run",
        input={"messages": []},
        context={"customer": "chosen"},
    )
    assert_type(native, NativeRunStream)
    agui = runtime.open_agui_run(
        thread_id="thread",
        run_id="run",
        messages=[{"role": "user", "content": "Hi", "id": "message"}],
        context={"customer": "chosen"},
    )
    assert_type(agui, AgUiRunStream)

    def resume(request: AgUiResumeRequest) -> AgUiRunStream:
        return runtime.open_agui_run(
            thread_id="thread",
            run_id="resume",
            resume=request,
            context={"customer": "chosen"},
        )

    runtime.open_run(
        thread_id="thread",
        run_id="run",
        input={"messages": []},
        context={"wrong": "field"},  # pyright: ignore[reportArgumentType]
    )
    runtime.open_agui_run(  # pyright: ignore[reportCallIssue]
        thread_id="thread",
        run_id="run",
        messages=[],
        input={"messages": []},  # pyright: ignore[reportArgumentType]
    )
    runtime.open_agui_run(
        thread_id="thread", run_id="run", messages=[], on_resume_saved=terminal
    )  # pyright: ignore[reportCallIssue, reportArgumentType]
    builder.open_run(thread_id="thread", run_id="run", input={"messages": []})  # pyright: ignore[reportAttributeAccessIssue]
    runtime.with_namespace("other")  # pyright: ignore[reportAttributeAccessIssue]
