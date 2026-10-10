"""对话任务命令复用业务权限、版本与可重放身份"""

from langgraph.runtime import ExecutionInfo
from test_automation_integration import automation_environment as automation_environment
from test_automation_integration import automation_resources as automation_resources
from test_automation_integration import automation_worker as automation_worker

from tinkerfin.tools import ToolRuntime
from tinkerfin_contracts import RunIdentity


def call_runtime(task_id: str = "node", call_id: str = "call") -> ToolRuntime:
    class ConversationToolRuntime(ToolRuntime):
        @property
        def identity(self) -> RunIdentity:
            return RunIdentity(namespace="ns_1", thread_id="chat", run_id="run")

    return ConversationToolRuntime(
        state={"messages": []},
        context=None,
        config={},
        stream_writer=lambda _: None,
        tool_call_id=call_id,
        store=None,
        execution_info=ExecutionInfo(
            task_id=task_id, checkpoint_id="checkpoint", checkpoint_ns=""
        ),
    )
