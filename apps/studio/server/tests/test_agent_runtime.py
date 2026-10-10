"""Studio Runtime 的模型配置、用户隔离与惰性工作区"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import create_autospec

from deepagents.backends import BackendProtocol, StateBackend
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import (
    FakeListChatModel,
)
from langchain_core.tools import BaseTool
from pydantic import Field

from tinkerfin_contracts import PreparedWorkspace, RunIdentity
from tinkerfin_sandbox import (
    RootedOpenSandboxBackend,
)


class _ToolModel(FakeListChatModel):
    """记录模型实际可调用的工具，并返回固定回复"""

    seen_tools: list[set[str]] = Field(default_factory=list)

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> BaseChatModel:
        self.seen_tools.append(
            {tool.name for tool in tools if isinstance(tool, BaseTool)}
        )
        return self


class _Workspace:
    """通过同步信号记录执行期间的用户工作区借用"""

    def __init__(
        self,
        failure: Exception | None = None,
        *,
        backend: BackendProtocol | None = None,
    ) -> None:
        self.opened: list[RunIdentity] = []
        self.closed: list[RunIdentity] = []
        self.failure = failure
        self.backend = backend if backend is not None else StateBackend()

    def with_directories(self, directories):
        return self

    @asynccontextmanager
    async def prepare(
        self, identity: RunIdentity
    ) -> AsyncGenerator[PreparedWorkspace[RootedOpenSandboxBackend, BackendProtocol]]:
        self.opened.append(identity)
        try:
            if self.failure is not None:
                raise self.failure
            yield PreparedWorkspace(
                workspace=create_autospec(RootedOpenSandboxBackend, instance=True),
                backend=self.backend,
            )
        finally:
            self.closed.append(identity)
