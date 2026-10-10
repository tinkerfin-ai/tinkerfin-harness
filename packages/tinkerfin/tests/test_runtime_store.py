"""Runtime Store isolation through the public execution and LangGraph Store APIs."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool


class _Model(FakeMessagesListChatModel):
    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> Runnable:
        del tools, kwargs
        return self
