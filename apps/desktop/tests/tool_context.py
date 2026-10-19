"""本机工具测试借用真实工作区，不创建 Agent 或 Graph 状态"""

from collections.abc import Iterator
from contextlib import contextmanager

from pydantic import JsonValue

from tinkerfin_contracts import ToolContext
from tinkerfin_contracts.workspace_tools import ToolExecutionScope, borrow_tool_context
from tinkerfin_remote import WorkspaceRequest
from tinkerfin_sandbox import LocalSession


@contextmanager
def tool_context(
    workspace: LocalSession, request: WorkspaceRequest, invocation_id: str
) -> Iterator[ToolContext[LocalSession]]:
    outputs: list[JsonValue] = []

    async def emit(value: JsonValue) -> None:
        outputs.append(value)

    scope = ToolExecutionScope(
        identity=request.identity,
        origin_identity=request.identity,
        workspace_scope_id=request.workspace_scope_id,
        invocation_id=invocation_id,
        graph_namespace=(),
        purpose="tool",
        tool_call_id=invocation_id,
    )
    with borrow_tool_context(workspace, scope, emit) as context:
        yield context
