"""Studio 工具失败反馈与每次自主执行的调用预算"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from httpx import HTTPError
from langchain.agents.middleware import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
    ToolCallLimitMiddleware,
    ToolRetryMiddleware,
)
from langchain_core.tools import BaseTool, ToolException

from tinkerfin_sandbox import (
    OpenSandboxBackendTimeoutError,
    OpenSandboxBackendUnavailableError,
    OpenSandboxFileTooLargeError,
)
from tinkerfin_studio.api.errors import BusinessException

TOOL_CALL_LIMIT = 24


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
    ToolCallLimitMiddleware[None, None],
]:
    """配置工具失败反馈与执行上限，不自动重试

    可预期异常只返回安全说明；未知异常、取消和审批中断继续传播。
    每次执行最多允许 24 个新工具调用，成功与失败均计入，超出后直接结束。
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
        ToolCallLimitMiddleware(run_limit=TOOL_CALL_LIMIT, exit_behavior="end"),
    )
