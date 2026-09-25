"""会话与自动化共享的文件审批选择"""

from typing import Literal, TypeAlias

from langchain.agents.middleware import InterruptOnConfig

AccessMode: TypeAlias = Literal["full", "write_approval"]


def file_review_policy(access_mode: AccessMode) -> dict[str, bool | InterruptOnConfig]:
    """为脚本和文件写入配置相同审批，不扩大工作区权限或批准整份计划"""
    if access_mode == "full":
        return {}
    if access_mode != "write_approval":
        raise ValueError("访问模式无效")
    return {
        name: {
            "allowed_decisions": ["approve", "reject"],
            "description": "需要人工审批：Agent 正准备运行命令或写入文件",
        }
        for name in (
            "execute",
            "write_file",
            "edit_file",
            "delete",
            "import_attachment",
            "create_file",
            "generate_image",
            "capture_browser",
            "deliver_file",
            "deliver_automation_files",
        )
    }
