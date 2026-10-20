"""当前技能文件准备与用户选择消息"""

from collections.abc import Callable, Sequence
from typing import Any

import pytest
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool
from pydantic import Field

pytestmark = pytest.mark.usefixtures("projects")


class RecordingModel(FakeMessagesListChatModel):
    prompts: list[str] = Field(default_factory=list)
    inputs: list[list[BaseMessage]] = Field(default_factory=list)

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> "RecordingModel":
        return self

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.inputs.append(messages)
        self.prompts.append("\n".join(str(message.content) for message in messages))
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="done"))]
        )
