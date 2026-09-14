"""Reloaded readers recover committed text before the producer completes."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from pydantic import Field

from tinkerfin import TinkerFin
from tinkerfin.agui import AgUiHistory
from tinkerfin_messaging import Messaging
from tinkerfin_tracing import Tracer


class PausedModel(FakeListChatModel):
    release: asyncio.Event = Field(default_factory=asyncio.Event, exclude=True)
    started: int = 0

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> Runnable:
        return self

    async def _astream(
        self, *args: Any, **kwargs: Any
    ) -> AsyncIterator[ChatGenerationChunk]:
        self.started += 1
        yield ChatGenerationChunk(message=AIMessageChunk(content="first", id="answer"))
        await self.release.wait()
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content=" second", id="answer", chunk_position="last"
            )
        )


def _event(chunk: bytes) -> dict[str, Any]:
    return json.loads(
        next(
            line[6:]
            for line in chunk.decode().splitlines()
            if line.startswith("data: ")
        )
    )


@pytest.mark.parametrize("finish_during_attach", [False, True])
async def test_reload_replays_partial_text_without_reexecuting_model(
    finish_during_attach: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = PausedModel(responses=["unused"])
    tracer = Tracer()
    runtime = (
        TinkerFin().with_namespace("reload").with_observer(tracer).build(model=model)
    )
    async with Messaging() as messaging:
        channel = messaging.agui_channel(name="events")
        started: list[str] = []
        finished: list[str] = []
        committed_terminal = asyncio.Event()

        async def on_started(event):
            started.append(event.run_id)

        async def on_finished(event):
            finished.append(event.run_id)
            committed_terminal.set()

        owner = await channel.open_sse(
            runtime.open_agui_run(
                thread_id="thread",
                run_id="run",
                messages=[{"id": "human", "role": "user", "content": "hello"}],
            ),
            after=0,
            on_run_started=on_started,
            on_run_finished=on_finished,
        )
        try:
            async for chunk in owner:
                if _event(chunk)["type"] == "TEXT_MESSAGE_CONTENT":
                    break
        finally:
            await owner.aclose()
        if finish_during_attach:
            follow = channel.follow_sse

            async def finish_before_baseline(*, identity, last_event_id=None):
                source = await follow(identity=identity, last_event_id=last_event_id)
                model.release.set()
                await committed_terminal.wait()
                return source

            monkeypatch.setattr(channel, "follow_sse", finish_before_baseline)
        live = await AgUiHistory(tracer, namespace="reload").open_live(
            "thread", channel=channel
        )
        try:
            assert any(
                m.role == "user" and m.content == "hello"
                for m in live.history.snapshot.messages
            )
            assert not any(
                m.role == "assistant" for m in live.history.snapshot.messages
            )
            assert live.body is not None
            data = []
            async for chunk in live.body:
                event = _event(chunk)
                data.append(event)
                if event["type"] == "TEXT_MESSAGE_CONTENT":
                    assert event["delta"] == "first"
                    assert b"event: replay\n" in chunk
                    break
            assert model.release.is_set() is finish_during_attach
            assert model.started == 1
            model.release.set()
            async for chunk in live.body:
                assert b"event: replay\n" not in chunk
                data.append(_event(chunk))
            assert (
                "".join(e["delta"] for e in data if e["type"] == "TEXT_MESSAGE_CONTENT")
                == "first second"
            )
            assert started == ["run"]
            assert finished == ["run"]
            assert model.started == 1
        finally:
            model.release.set()
            await live.aclose()
