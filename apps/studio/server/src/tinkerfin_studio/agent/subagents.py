"""读取 Studio 的子智能体业务配置"""

from __future__ import annotations

from pathlib import Path

import yaml
from anyio import to_thread
from pydantic import BaseModel, ConfigDict, Field, RootModel


class SubagentSettings(BaseModel):
    """子智能体的用途、提示词与可用业务工具"""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1)
    system_prompt: str = Field(min_length=1)
    tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(
        default_factory=list,
        description="从当前用户启用技能中按名称选择，未列出的技能不提供给该子智能体",
    )


class _SubagentFile(RootModel[dict[str, SubagentSettings]]):
    """以子智能体名称为键的完整配置文件"""


def _read_subagents() -> dict[str, SubagentSettings]:
    path = Path(__file__).with_name("subagents.yaml")
    return _SubagentFile.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    ).root


async def load_subagents() -> dict[str, SubagentSettings]:
    """启动时读取子智能体配置，避免会话执行重复访问配置文件

    文件读取使用 AnyIO 默认容量限制的工作线程；没有独立超时，取消会等待读取结束。
    解析失败会阻止应用启动，不生成不完整的子智能体配置。

    Returns:
        通过校验的子智能体名称与配置

    Raises:
        OSError: 配置文件不可读
        ValueError: 配置内容不符合声明
        yaml.YAMLError: 配置不是合法 YAML
    """

    return await to_thread.run_sync(_read_subagents)


__all__ = ["SubagentSettings", "load_subagents"]
