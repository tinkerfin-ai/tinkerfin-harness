"""Planning analysis shares workspace access, host policies, and durable tool review."""

import asyncio
from typing import Literal

import pytest
from ag_ui.core import RunErrorEvent, StateSnapshotEvent, ToolCallResultEvent
from deepagents.backends import CompositeBackend, StateBackend, StoreBackend
from deepagents.backends.protocol import ExecuteResponse, SandboxBackendProtocol
from deepagents.backends.utils import create_file_data
from langchain.agents.middleware import ToolCallLimitMiddleware, ToolRetryMiddleware
from langchain.agents.middleware.types import AgentMiddleware, AgentState
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.runtime import Runtime
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command
from test_plan_mode import (
    _agui_events,
    _FakeModel,
    _interrupt_outcome,
    _parts,
    _planner,
    _root_interrupts,
    _root_values,
    _structured_plan_state,
)
from test_runtime_workspace import _Workspace

from tinkerfin import AgUiResumeRequest, TinkerFin


class _AnalysisSandbox(StateBackend, SandboxBackendProtocol):
    """Record execution through the public sandbox boundary without starting processes."""

    def __init__(self) -> None:
        super().__init__()
        self.commands: list[str] = []

    @property
    def id(self) -> str:
        return "planning-analysis"

    async def aexecute(
        self, command: str, *, timeout: int | None = None
    ) -> ExecuteResponse:
        self.commands.append(command)
        return ExecuteResponse(output="analysis result", exit_code=0)


def _execute(*commands: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": "execute", "args": {"command": command}, "id": f"analysis-{index}"}
            for index, command in enumerate(commands)
        ],
    )


async def test_planner_analysis_preserves_workspace_skills_memory_and_host_prompt() -> (
    None
):
    store = InMemoryStore()
    for path, content in {
        "/documents/SKILL.md": (
            "---\nname: documents\ndescription: Inspect document evidence\n---\n"
            "Extract data before making claims."
        ),
        "/context.md": "Use the customer's accounting definitions.",
    }.items():
        await store.aput(
            ("dGVzdA", "planning-skills"), path, dict(create_file_data(content))
        )
    sandbox = _AnalysisSandbox()
    workspace = _Workspace(
        CompositeBackend(
            default=sandbox,
            routes={"/skills/": StoreBackend(namespace=lambda _: ("planning-skills",))},
        )
    )
    model = _FakeModel(responses=[_execute("python analyze.py"), _planner()])
    runtime = (
        TinkerFin(checkpointer=InMemorySaver(), store=store)
        .with_namespace("test")
        .with_compaction_tool()
        .with_plan(enabled=True)
        .build(
            model=model,
            backend=workspace,
            system_prompt="Cite observed evidence in the customer's language.",
            skills=["/skills/"],
            memory=["/skills/context.md"],
        )
    )
    events = await _agui_events(
        runtime,
        {"messages": [HumanMessage(content="Analyze then plan", id="request")]},
        run_id="analysis",
        config={},
        mode="plan",
    )
    assert sandbox.commands == ["python analyze.py"]
    assert _interrupt_outcome(events).interrupts[0].reason == "tinkerfin:plan_review"
    system = model.model_inputs[0][0].text
    assert "Cite observed evidence" in system
    assert "Inspect document evidence" in system
    assert "accounting definitions" in system
    assert "File paths are relative to the selected workspace" in system
    assert "analysis result" in str(model.model_inputs[1])
    assert "compact_conversation" in model.bound_tool_names[0]
    assert workspace.opened == workspace.closed


@pytest.mark.parametrize("decision", ["approve", "reject", "mixed", "abandon"])
async def test_planning_tool_review_survives_rebuild_and_does_not_approve_plan(
    decision: Literal["approve", "reject", "mixed", "abandon"],
) -> None:
    saver = InMemorySaver()
    sandbox = _AnalysisSandbox()
    workspace = _Workspace(sandbox)
    first_model = _FakeModel(responses=[_execute("inspect first", "inspect second")])

    def build(model: _FakeModel):
        return (
            TinkerFin(checkpointer=saver)
            .with_namespace("test")
            .with_plan(enabled=True)
            .build(model=model, backend=workspace, interrupt_on={"execute": True})
        )

    initial = await _agui_events(
        build(first_model),
        {"messages": [HumanMessage(content="Investigate", id="request")]},
        run_id="analysis-request",
        config={},
        mode="plan",
    )
    pending = _interrupt_outcome(initial).interrupts
    assert len(pending) == 2 and all(item.reason == "tool_call" for item in pending)
    assert sandbox.commands == []
    request = AgUiResumeRequest.model_validate(
        {
            "entries": [
                {"interruptId": item.id, "status": "cancelled"}
                if decision == "abandon" or (decision == "mixed" and index == 1)
                else {
                    "interruptId": item.id,
                    "status": "resolved",
                    "payload": {
                        "type": "reject" if decision == "reject" else "approve"
                    },
                }
                for index, item in enumerate(pending)
            ]
        }
    )
    resumed_model = _FakeModel(responses=[_planner()])
    runtime = build(resumed_model)
    resumed = await _agui_events(
        runtime,
        None,
        run_id="analysis-decision",
        config={},
        mode="default",
        resume=request,
    )
    if decision == "abandon":
        assert isinstance(resumed[-1], RunErrorEvent)
        assert resumed[-1].code == "resume_cancelled"
        assert sandbox.commands == [] and not resumed_model.model_inputs
    else:
        expected = {
            "approve": ["inspect first", "inspect second"],
            "reject": [],
            "mixed": ["inspect first"],
        }[decision]
        assert sorted(sandbox.commands) == expected
        review = _interrupt_outcome(resumed).interrupts[0]
        assert review.reason == "tinkerfin:plan_review"
        snapshots = [item for item in resumed if isinstance(item, StateSnapshotEvent)]
        plan = _structured_plan_state(snapshots[-1].snapshot["tinkerfin_plan"])
        assert plan.status == "awaiting_review" and plan.effective_mode == "plan"
        assert plan.confirmed_plan is None and plan.handoff is None
        results = [item for item in resumed if isinstance(item, ToolCallResultEvent)]
        assert {item.tool_call_id for item in pending} <= {
            item.tool_call_id for item in results
        }
        calls = len(resumed_model.model_inputs)
        await _agui_events(
            build(resumed_model),
            None,
            run_id="analysis-decision",
            config={},
            mode="default",
            resume=request,
        )
        assert sorted(sandbox.commands) == expected
        assert len(resumed_model.model_inputs) == calls
    assert workspace.opened == workspace.closed


async def test_native_planning_tool_approval_continues_planning() -> None:
    sandbox = _AnalysisSandbox()
    model = _FakeModel(responses=[_execute("inspect evidence"), _planner()])
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(model=model, backend=_Workspace(sandbox), interrupt_on={"execute": True})
    )
    config = {"configurable": {"thread_id": "plan-thread"}}
    first = await _parts(
        runtime,
        {"messages": [HumanMessage(content="Plan", id="request")]},
        run_id="native-analysis",
        config=config,
        mode="plan",
    )
    assert _root_interrupts(first) and not sandbox.commands
    resumed = await _parts(
        runtime,
        Command(resume={"decisions": [{"type": "approve"}]}),
        run_id="native-analysis-decision",
        config=config,
        mode="default",
    )
    assert sandbox.commands == ["inspect evidence"]
    assert _root_interrupts(resumed)[0].value["kind"] == "tinkerfin:plan_review"


@pytest.mark.parametrize("rewrite_by_host", [False, True])
async def test_invalid_plan_and_analysis_batch_is_corrected_before_tool_review(
    rewrite_by_host: bool,
) -> None:
    sandbox = _AnalysisSandbox()
    proposal = _planner()
    analysis = _execute("inspect evidence").tool_calls
    if not rewrite_by_host:
        proposal.tool_calls.extend(analysis)

    class AddAnalysis(AgentMiddleware):
        def after_model(self, state: AgentState, runtime: Runtime):
            last = state["messages"][-1]
            if isinstance(last, AIMessage) and last.id == proposal.id:
                return {
                    "messages": [
                        last.model_copy(
                            update={"tool_calls": [*last.tool_calls, *analysis]}
                        )
                    ]
                }
            return None

    model = _FakeModel(responses=[proposal, _planner(suffix="-corrected")])
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(
            model=model,
            backend=_Workspace(sandbox),
            interrupt_on={"execute": True},
            middleware=[AddAnalysis()] if rewrite_by_host else [],
        )
    )
    events = await _agui_events(
        runtime,
        {"messages": [HumanMessage(content="Plan", id="request")]},
        run_id="invalid-batch",
        config={},
        mode="plan",
    )
    assert not sandbox.commands
    assert _interrupt_outcome(events).interrupts[0].reason == "tinkerfin:plan_review"
    errors = [item for item in model.model_inputs[1] if isinstance(item, ToolMessage)]
    assert len(errors) == 2 and all(item.status == "error" for item in errors)


async def test_planner_uses_host_error_policy_and_stops_at_its_tool_budget() -> None:
    executed: list[str] = []

    async def inspect_document() -> str:
        """Inspect one document with a correctable input failure."""
        executed.append("inspect")
        raise ValueError("private parser diagnostics")

    model = _FakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "inspect_document", "args": {}, "id": f"read-{i}"}
                ],
            )
            for i in range(3)
        ]
    )
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(
            model=model,
            tools=[inspect_document],
            middleware=[
                ToolRetryMiddleware(
                    max_retries=0,
                    retry_on=(ValueError,),
                    on_failure=lambda _: "Choose another document",
                ),
                ToolCallLimitMiddleware(run_limit=2, exit_behavior="end"),
            ],
        )
    )
    parts = await _parts(
        runtime,
        {"messages": [HumanMessage(content="Plan", id="request")]},
        run_id="bounded-analysis",
        config={"configurable": {"thread_id": "plan-thread"}},
        mode="plan",
    )
    assert executed == ["inspect", "inspect"]
    assert "Choose another document" in str(model.model_inputs[1])
    assert "private parser diagnostics" not in str(parts)
    assert (
        _structured_plan_state(_root_values(parts)[-1]["tinkerfin_plan"]).status
        == "awaiting_input"
    )


async def test_cancelling_planning_analysis_settles_before_releasing_workspace() -> (
    None
):
    started = asyncio.Event()
    settled = asyncio.Event()

    class WaitingAnalysis(_AnalysisSandbox):
        async def aexecute(
            self, command: str, *, timeout: int | None = None
        ) -> ExecuteResponse:
            started.set()
            try:
                await asyncio.Event().wait()
                raise AssertionError("analysis must be cancelled")
            finally:
                settled.set()

    workspace = _Workspace(WaitingAnalysis())
    model = _FakeModel(responses=[_execute("inspect evidence")])
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(model=model, backend=workspace)
    )
    stream = runtime.open_run(
        thread_id="planning-cancellation",
        run_id="analysis",
        mode="plan",
        input={"messages": [HumanMessage(content="Analyze", id="request")]},
    )

    async def consume() -> None:
        async for _part in stream:
            pass

    consuming = asyncio.create_task(consume())
    try:
        await started.wait()
        consuming.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consuming
        assert settled.is_set()
    finally:
        await stream.aclose()
        if not consuming.done():
            consuming.cancel()
        await asyncio.gather(consuming, return_exceptions=True)
    assert workspace.opened == workspace.closed
    assert len(model.model_inputs) == 1


@pytest.mark.parametrize("image_inputs", [False, True])
async def test_planner_default_media_gate_uses_routed_model_for_system_images(
    image_inputs: bool,
) -> None:
    initial = _FakeModel(
        responses=[AIMessage(content="unused")],
        profile={"image_inputs": not image_inputs},
    )
    destination = _FakeModel(
        responses=[_planner()], profile={"image_inputs": image_inputs}
    )

    class RouteModel(AgentMiddleware):
        async def awrap_model_call(self, request, handler):
            return await handler(request.override(model=destination))

    system = SystemMessage(
        content=[
            {"type": "text", "text": "Use the supplied chart for planning."},
            {"type": "image", "base64": "cG5n", "mime_type": "image/png"},
        ]
    )
    original = system.model_copy(deep=True)
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(model=initial, system_prompt=system, middleware=[RouteModel()])
    )
    events = await _agui_events(
        runtime,
        {"messages": [HumanMessage(content="Plan", id="request")]},
        run_id="system-image",
        config={},
        mode="plan",
    )
    assert _interrupt_outcome(events).interrupts[0].reason == "tinkerfin:plan_review"
    assert not initial.model_inputs and len(destination.model_inputs) == 1
    assert ("cG5n" in str(destination.model_inputs)) is image_inputs
    assert "Use the supplied chart" in str(destination.model_inputs)
    assert system == original


@pytest.mark.parametrize("workspace_context", [False, True])
@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize(
    "mime_type",
    [
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ],
)
async def test_planner_preserves_system_file_format_before_the_routed_model_gate(
    workspace_context: bool, mime_type: str, wrapped: bool
) -> None:
    initial = _FakeModel(
        responses=[AIMessage(content="unused")], profile={"pdf_inputs": False}
    )
    destination = _FakeModel(responses=[_planner()], profile={"pdf_inputs": True})
    block = {
        "type": "file",
        "file": {
            "file_data": f"data:{mime_type};base64,cGxhbi1maWxl",
            "filename": "report",
        },
    }
    if wrapped:
        block = {"type": "non_standard", "value": block}
    system = SystemMessage(content=[block])

    class RouteModel(AgentMiddleware):
        async def awrap_model_call(self, request, handler):
            assert request.system_message is not None
            assert isinstance(request.system_message.content, list)
            assert block in request.system_message.content
            return await handler(request.override(model=destination))

    backend = StateBackend()
    runtime = (
        TinkerFin(checkpointer=InMemorySaver())
        .with_namespace("test")
        .with_plan(enabled=True)
        .build(
            model=initial,
            system_prompt=system,
            backend=_Workspace(backend) if workspace_context else backend,
            skills=["/skills/"] if workspace_context else None,
            memory=["/context.md"] if workspace_context else None,
            middleware=[RouteModel()],
        )
    )
    events = await _agui_events(
        runtime,
        {
            "messages": [HumanMessage(content="Plan", id="request")],
            "files": {
                "/skills/audit/SKILL.md": create_file_data(
                    "---\nname: audit\ndescription: Inspect audit facts\n---\nUse observed evidence."
                ),
                "/context.md": create_file_data("Use accounting definitions."),
            },
        },
        run_id="system-file",
        config={},
        mode="plan",
    )
    assert _interrupt_outcome(events).interrupts[0].reason == "tinkerfin:plan_review"
    assert not initial.model_inputs
    assert ("cGxhbi1maWxl" in str(destination.model_inputs)) is (
        mime_type == "application/pdf"
    )
    assert "tinkerfin_native_media" not in str(destination.model_inputs)
    assert system.content == [block]
