"""会话运行的本机操作确认策略"""

from typing import Literal, TypeAlias

from langchain.agents.middleware import InterruptOnConfig

AccessMode: TypeAlias = Literal["full", "write_approval"]


def local_review_policy(access_mode: AccessMode) -> dict[str, bool | InterruptOnConfig]:
    """按会话选择确认本机副作用，云端工具与计划审阅各自独立"""
    if access_mode == "full":
        return {}
    if access_mode != "write_approval":
        raise ValueError("本机确认模式无效")
    return {
        f"computer_{name}": {
            "allowed_decisions": ["approve", "reject"],
            "description": "确认在本机执行此操作",
        }
        for name in (
            "execute",
            "write_file",
            "edit_file",
            "delete",
            "import_attachment",
            "open_url",
            "call_mcp_tool",
            "desktop_press",
            "desktop_set_value",
        )
    }
