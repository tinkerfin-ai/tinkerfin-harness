"""会话与自动化共享的文件审批选择"""

from typing import Literal, TypeAlias

from langchain.agents.middleware import InterruptOnConfig

AccessMode: TypeAlias = Literal["full", "write_approval"]


def file_review_policy(access_mode: AccessMode) -> dict[str, bool | InterruptOnConfig]:
    """仅决定写文件工具是否需要审批，不授予额外的工作区访问权限"""
    if access_mode == "full":
        return {}
    if access_mode != "write_approval":
        raise ValueError("访问模式无效")
    return {
        "write_file": {
            "allowed_decisions": ["approve", "reject"],
            "description": "需要人工审批：Agent 正准备写入文件",
        }
    }
