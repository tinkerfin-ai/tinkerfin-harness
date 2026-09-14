"""Studio 工具失败反馈与每次自主执行的调用预算"""

from __future__ import annotations

from httpx import HTTPError
from langchain.agents.middleware import ToolCallLimitMiddleware, ToolRetryMiddleware
from langchain_core.tools import ToolException

from tinkerfin_sandbox import (
    OpenSandboxBackendTimeoutError,
    OpenSandboxBackendUnavailableError,
    OpenSandboxFileTooLargeError,
)
from tinkerfin_studio.api.errors import BusinessException

TOOL_CALL_LIMIT = 24


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


def tool_execution_policy() -> tuple[
    ToolRetryMiddleware[None, None], ToolCallLimitMiddleware[None, None]
]:
    """配置工具失败反馈与执行上限，不自动重试

    可预期异常只返回安全说明；未知异常、取消和审批中断继续传播。
    每次执行最多允许 24 个新工具调用，成功与失败均计入，超出后直接结束。
    新消息或审批恢复开始新预算，恢复时已有的待执行调用不重复计数。
    计数和错误结果由原生中间件维护，不在业务对象上保存运行状态。

    Returns:
        可用于主智能体与各子智能体的相同业务策略
    """
    return (
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
