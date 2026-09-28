"""为可恢复的业务工具操作生成稳定身份"""

import json
from uuid import NAMESPACE_URL, uuid5

from langchain_core.tools import ToolException

from tinkerfin.tools import ToolRuntime


def tool_request_id(runtime: ToolRuntime, *, user_id: int, operation: str) -> str:
    """按用户、会话和原生调用身份区分操作，恢复不使用变化后的运行 ID"""
    info = runtime.execution_info
    if info is None or not runtime.tool_call_id:
        raise ToolException("操作缺少调用标识，请重新发起对话")
    identity = [
        str(user_id),
        runtime.identity.thread_id,
        operation,
        info.task_id,
        runtime.tool_call_id,
    ]
    return str(uuid5(NAMESPACE_URL, json.dumps(identity, ensure_ascii=False)))
