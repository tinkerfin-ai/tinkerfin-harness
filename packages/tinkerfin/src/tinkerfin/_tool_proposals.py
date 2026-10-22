"""Select outstanding calls without treating completed history as new work."""

from collections.abc import Sequence

from langchain_core.messages import AIMessage, BaseMessage, ToolCall, ToolMessage


def pending_tool_calls(messages: Sequence[BaseMessage]) -> list[ToolCall]:
    """Match LangChain 1.4.2's model-to-tools routing for the latest proposal.

    Middleware may answer invalid calls before resource preparation or review.
    Only results following the latest AI message complete its calls; historical
    results cannot suppress a new proposal that reuses a tool-call identifier.
    """
    answered: set[str] = set()
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            return [call for call in message.tool_calls if call["id"] not in answered]
        if isinstance(message, ToolMessage):
            answered.add(message.tool_call_id)
    return []
