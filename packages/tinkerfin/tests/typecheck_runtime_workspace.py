"""Tools preserve business context, workspace, and state type parameters."""

from pathlib import Path
from typing import TypedDict, assert_type

from deepagents import DeepAgentState
from deepagents.backends.protocol import BackendProtocol
from langchain_core.tools import tool

from tinkerfin import AgentRuntime, TinkerFin
from tinkerfin.subagents import SubAgent
from tinkerfin.tools import ToolRuntime
from tinkerfin_contracts import Workspace


class Context(TypedDict):
    customer: str


class State(DeepAgentState):
    account: str


@tool
async def read_files(runtime: ToolRuntime[Context, Path]) -> str:
    """Read the workspace name."""
    assert_type(runtime.workspace, Path)
    assert_type(runtime.identity.namespace, str)
    assert_type(runtime.context, Context)
    assert_type(runtime.state, DeepAgentState)
    return runtime.workspace.name


def inspect_state(runtime: ToolRuntime[Context, Path, State]) -> None:
    assert_type(runtime.state, State)
    assert_type(runtime.state["account"], str)


def build(workspace: Workspace[Path, BackendProtocol]) -> None:
    reader: SubAgent = {
        "name": "reader",
        "description": "Read workspace reports",
        "system_prompt": "Read reports",
        "tools": [read_files],
    }
    builder = TinkerFin().with_namespace("company")
    runtime = builder.build(
        model="openai:example",
        backend=workspace,
        tools=[read_files],
        subagents=[reader],
        context_schema=Context,
    )
    assert_type(runtime, AgentRuntime[Context])
    runtime.open_run(
        thread_id="thread",
        run_id="run",
        input={"messages": []},
        context={"customer": "customer"},
    )
    assert_type(builder.build(model="openai:example"), AgentRuntime[None])
