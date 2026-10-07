"""Studio 工具失败反馈与每次自主执行的调用预算"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, NotRequired

from httpx import HTTPError
from langchain.agents.middleware import (
    AgentMiddleware,
    AgentState,
    ModelRequest,
    ModelResponse,
    ToolRetryMiddleware,
)
from langchain.agents.middleware.types import PrivateStateAttr, hook_config
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool, ToolException
from langgraph.channels.untracked_value import UntrackedValue
from langgraph.runtime import Runtime

from tinkerfin_sandbox import (
    OpenSandboxBackendTimeoutError,
    OpenSandboxBackendUnavailableError,
    OpenSandboxFileTooLargeError,
)
from tinkerfin_studio.api.errors import BusinessException

TOOL_CALL_LIMIT = 24
_FINISHING_RESERVE = 6


class ToolBudgetState(AgentState[None]):
    """记录本轮已申请的工具额度，不跨用户消息或审批恢复继承"""

    studio_tool_calls_used: NotRequired[
        Annotated[int, UntrackedValue, PrivateStateAttr]
    ]


class ToolBudget(AgentMiddleware[ToolBudgetState, None, None]):
    """限制自主执行次数，并保留一次只整理已有结果的模型调用

    主智能体和每次子任务各自拥有额度。整批提议超限时不执行其中任何工具，
    逐项返回未执行结果，再让模型总结；总结阶段不提供工具且不允许再次调用。
    计数保存在本次执行状态中，不在中间件实例中共享，审批恢复不重复计数。
    """

    state_schema = ToolBudgetState

    async def awrap_model_call(
        self,
        request: ModelRequest[None],
        handler: Callable[[ModelRequest[None]], Awaitable[ModelResponse[None]]],
    ) -> ModelResponse[None]:
        """告诉模型剩余额度，耗尽后只允许基于已有工具结果作最终交付"""
        used = request.state.get("studio_tool_calls_used", 0)
        if not isinstance(used, int) or used < 0:
            raise ValueError("工具额度状态无效")
        remaining = max(0, TOOL_CALL_LIMIT - used)
        if remaining == 0:
            guidance = (
                "本轮工具额度已用尽。现在必须只用已有信息返回最终总结，不再调用工具。"
                "明确列出已完成的工作、关键结论与来源、已有文件及未完成事项；"
                "被阻止的操作没有执行，不得宣称成功。若你在执行委派任务，"
                "必须把已有研究结论返回给主智能体，不能只返回额度提示。"
            )
        else:
            guidance = (
                f"本轮最多可申请{TOOL_CALL_LIMIT}次工具调用，现在剩余{remaining}次；"
                "每个并行调用各计一次，失败也计入，单批不得超过剩余额度。"
                f"请为必要的产物生成、检查、修正和交付预留{_FINISHING_RESERVE}次，"
                "已有足够证据就停止搜索并整合结果。委派时明确范围、交付内容及"
                "停止条件，收到子任务结果后仅补查明确缺口。"
            )
            if remaining <= _FINISHING_RESERVE:
                guidance += "现在进入收尾阶段，不再扩大研究或新建子任务，优先完成交付。"
        system = request.system_message
        if system is None:
            system = SystemMessage(content=guidance)
        else:
            content = system.content
            system = system.model_copy(
                update={
                    "content": (
                        f"{content}\n\n{guidance}"
                        if isinstance(content, str)
                        else [*content, {"type": "text", "text": guidance}]
                    )
                }
            )
        configured = request.override(system_message=system)
        if remaining == 0:
            configured = configured.override(tools=[], tool_choice="none")
        response = await handler(configured)
        if remaining == 0:
            answers = [item for item in response.result if isinstance(item, AIMessage)]
            if any(item.tool_calls for item in answers) or not any(
                item.text.strip() for item in answers
            ):
                raise ValueError("工具额度耗尽后模型未返回有效总结")
        return response

    @hook_config(can_jump_to=["model"])
    async def aafter_model(
        self, state: ToolBudgetState, runtime: Runtime[None]
    ) -> dict[str, int | str | list[ToolMessage]] | None:
        """在工具执行前整批核算，超限提议均返回结果并转入总结"""
        message = next(
            (
                item
                for item in reversed(state["messages"])
                if isinstance(item, AIMessage)
            ),
            None,
        )
        if message is None or not message.tool_calls:
            return None
        used = state.get("studio_tool_calls_used", 0) + len(message.tool_calls)
        if used <= TOOL_CALL_LIMIT:
            return {"studio_tool_calls_used": used}
        return {
            "studio_tool_calls_used": used,
            "jump_to": "model",
            "messages": [
                ToolMessage(
                    content="本批工具调用超过本轮剩余额度，整批均未执行。请总结已有结果和未完成事项。",
                    tool_call_id=call["id"],
                    name=call["name"],
                    status="error",
                )
                for call in message.tool_calls
            ],
        }


class ServiceAvailability(AgentMiddleware):
    """按本次绑定展示搜索和生图工具，保留待审批调用的执行注册"""

    def __init__(self, *, search_available: bool, image_available: bool) -> None:
        self._unavailable = {
            name
            for name, available in (
                ("web_search", search_available),
                ("generate_image", image_available),
            )
            if not available
        }

    async def awrap_model_call(
        self,
        request: ModelRequest[None],
        handler: Callable[[ModelRequest[None]], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        if self._unavailable:
            request = request.override(
                tools=[
                    item
                    for item in request.tools
                    if not (
                        isinstance(item, BaseTool) and item.name in self._unavailable
                    )
                ]
            )
        return await handler(request)


def _failure_message(error: Exception) -> str:
    if isinstance(error, ToolException):
        return "工具未完成，请检查输入或换用可行方案，不要重复相同调用"
    if isinstance(error, BusinessException):
        return error.error_code.message
    if isinstance(error, PermissionError):
        return "工具无权执行此操作，请使用已授权的资源，不要重复相同调用"
    if isinstance(error, (HTTPError, TimeoutError, ConnectionError)):
        return (
            "工具服务暂不可用或响应超时；操作结果可能未确认，请先核实，勿重复生成或交付"
        )
    if isinstance(error, OpenSandboxFileTooLargeError):
        return "文件超过读取大小限制，请缩小文件或选择其他结果"
    if isinstance(
        error, (OpenSandboxBackendTimeoutError, OpenSandboxBackendUnavailableError)
    ):
        return "工作区暂不可用或响应超时，请核实已有产物，勿重复生成或交付"
    return "工具校验未通过，请检查参数、文件内容或服务配置，调整后再调用"


def tool_execution_policy(
    *, image_generation_available: bool = True, web_search_available: bool = True
) -> tuple[
    ServiceAvailability,
    ToolRetryMiddleware[None, None],
    ToolBudget,
]:
    """配置工具失败反馈与执行上限，不自动重试

    可预期异常只返回安全说明；未知异常、取消和审批中断继续传播。
    每次执行最多允许 24 个新工具调用，成功与失败均计入，耗尽后只做最终总结。
    新消息或审批恢复开始新预算，恢复时已有的待执行调用不重复计数。

    Args:
        image_generation_available: 本次运行是否配置可用生图服务；不移除已注册工具
        web_search_available: 本次运行是否配置可用搜索服务；不移除已注册工具

    Returns:
        可用于主智能体与各子智能体的相同业务策略
    """
    return (
        ServiceAvailability(
            search_available=web_search_available,
            image_available=image_generation_available,
        ),
        ToolRetryMiddleware(
            max_retries=0,
            retry_on=(
                ToolException,
                BusinessException,
                ValueError,
                HTTPError,
                TimeoutError,
                ConnectionError,
                PermissionError,
                OpenSandboxBackendTimeoutError,
                OpenSandboxBackendUnavailableError,
                OpenSandboxFileTooLargeError,
            ),
            on_failure=_failure_message,
        ),
        ToolBudget(),
    )
