import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from typing import cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse
from starlette.types import Message, Scope

from tinkerfin import SseBody
from tinkerfin_studio.api.dependencies import SessionDep, get_session
from tinkerfin_studio.api.responses import trace_sse_response


class _TraceEvent(BaseModel):
    event_id: str


def _http_scope() -> Scope:
    return cast(
        Scope,
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/events",
            "raw_path": b"/events",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("test", 80),
        },
    )


async def test_framework_sse_body_survives_repeated_response_cancellation() -> None:
    """原生响应重复取消时必须等待框架流完成上游清理"""

    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    closed = asyncio.Event()

    async def content() -> AsyncGenerator[bytes, None]:
        try:
            started.set()
            await asyncio.Future()
            yield b"unreachable"
        finally:
            cleanup_started.set()
            await release_cleanup.wait()
            closed.set()

    iterator = content()
    body = SseBody(source_factory=lambda: iterator, close=iterator.aclose)

    async def receive() -> Message:
        await asyncio.Future()
        raise AssertionError("不可达")

    async def send(message: Message) -> None:
        del message

    response_task = asyncio.create_task(
        StreamingResponse(body, media_type="text/event-stream")(
            _http_scope(), receive, send
        )
    )
    await started.wait()
    response_task.cancel("首次取消")
    await cleanup_started.wait()
    response_task.cancel("重复取消")
    await asyncio.sleep(0)
    try:
        assert not response_task.done()
    finally:
        release_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await response_task
    assert closed.is_set()


async def test_trace_response_encodes_models_and_owns_source_cleanup() -> None:
    """业务路由只提供事件流，响应门面负责编码与关闭"""

    closed = asyncio.Event()

    async def content() -> AsyncGenerator[BaseModel, None]:
        try:
            yield _TraceEvent(event_id="event-1")
        finally:
            closed.set()

    application = FastAPI()

    @application.get("/trace", response_class=StreamingResponse)
    async def trace() -> StreamingResponse:
        return trace_sse_response(content())

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get("/trace")

    assert response.status_code == 200
    assert response.text == 'event: trace\ndata: {"event_id":"event-1"}\n\n'
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert closed.is_set()


async def test_session_dependency_closes_before_stream_content_starts() -> None:
    """请求会话必须在原生长流开始前完成归还"""

    released = asyncio.Event()

    async def session_override() -> AsyncIterator[AsyncSession]:
        try:
            yield cast(AsyncSession, object())
        finally:
            released.set()

    application = FastAPI()
    application.dependency_overrides[get_session] = session_override

    @application.get("/events", response_class=StreamingResponse)
    async def events(session: SessionDep) -> StreamingResponse:
        del session

        async def content() -> AsyncIterator[bytes]:
            assert released.is_set()
            yield b"data: ready\n\n"

        return StreamingResponse(content(), media_type="text/event-stream")

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get("/events")

    assert response.status_code == 200
    assert response.content == b"data: ready\n\n"
    assert released.is_set()
