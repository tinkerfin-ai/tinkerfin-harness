"""Declare agents available for task delegation."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, NotRequired, TypedDict

from deepagents.middleware.filesystem import FilesystemPermission
from langchain.agents.middleware import AgentMiddleware, InterruptOnConfig
from langchain.agents.structured_output import ResponseFormat
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool


class SubAgent(TypedDict):
    """Describe a delegated agent that receives an isolated task description.

    Omitted model, tools, permissions, and tool-review rules inherit from the main
    agent. Skills are role-specific and are not inherited by declared agents.
    The automatic general-purpose agent inherits the main agent's skills as well.
    Tools may request ``tinkerfin.tools.ToolRuntime`` to use the current run's
    borrowed workspace; tools must not retain that workspace beyond the run.
    """

    name: str
    description: str
    system_prompt: str
    model: NotRequired[str | BaseChatModel]
    tools: NotRequired[Sequence[BaseTool | Callable[..., Any] | dict[str, Any]]]
    middleware: NotRequired[Sequence[AgentMiddleware[Any, Any, Any]]]
    interrupt_on: NotRequired[dict[str, bool | InterruptOnConfig]]
    skills: NotRequired[Sequence[str]]
    permissions: NotRequired[Sequence[FilesystemPermission]]
    response_format: NotRequired[ResponseFormat[Any] | type | dict[str, Any]]


__all__ = ["SubAgent"]
