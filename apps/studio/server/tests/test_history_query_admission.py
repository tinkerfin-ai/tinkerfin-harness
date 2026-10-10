"""历史与运行续播的共享准入及首快照资源契约"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace, TracebackType
from typing import Literal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from test_conversation_history import _finish_trace, _open_trace, _register

from tinkerfin_contracts import RunIdentity, ThreadIdentity
from tinkerfin_studio.api.errors import (
    BusinessException,
    ConversationErrorCode,
    SystemException,
)
from tinkerfin_studio.conversation import history_queries as admission_module
from tinkerfin_studio.conversation.failures import ConversationFailureProjection
from tinkerfin_studio.conversation.history import ConversationHistoryService
from tinkerfin_studio.conversation.history_queries import HistoryQueryAdmission
from tinkerfin_studio.conversation.models import (
    ConversationRunRegistration,
)
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.conversation.todo_groups import (
    TodoGroupProjection,
)
from tinkerfin_tracing import Tracer

_Entry = Literal["history", "live"]
_THREAD = "admission-thread"
_RUN = "admission-run"


class _ReplayChannel:
    """使用可观察的本地订阅核验关闭，不创建生产者或后台任务"""

    def __init__(self) -> None:
        self.opened = asyncio.Event()
        self.closed_count = 0
        self.sources: list[AsyncGenerator[bytes, None]] = []

    async def follow_sse(
        self, *, identity: RunIdentity, last_event_id: str | None = None
    ) -> AsyncGenerator[bytes, None]:
        assert identity == RunIdentity(namespace="ns_1", thread_id=_THREAD, run_id=_RUN)
        assert last_event_id in {None, "0"}

        async def frames() -> AsyncGenerator[bytes, None]:
            try:
                # 激活订阅后才将所有权交给响应体，首项仅作为本地绑定信号
                yield b""
                yield b"id: 1\nevent: replay\ndata: {}\n\n"
            finally:
                self.closed_count += 1

        source = frames()
        await anext(source)
        self.sources.append(source)
        self.opened.set()
        return source

    async def aclose(self) -> None:
        for source in self.sources:
            await source.aclose()


_HistoryCase = tuple[Tracer, ConversationRepository, _ReplayChannel]


@pytest.fixture
async def history_case(session: AsyncSession) -> AsyncIterator[_HistoryCase]:
    tracer = Tracer(
        projections=(ConversationFailureProjection(), TodoGroupProjection())
    )
    repository = ConversationRepository(session)
    await _register(repository, user_id=1, thread_id=_THREAD, run_id=_RUN)
    context, trace_session = await _open_trace(tracer, thread_id=_THREAD, run_id=_RUN)
    channel = _ReplayChannel()
    try:
        yield tracer, repository, channel
    finally:
        await channel.aclose()
        await _finish_trace(context, trace_session)


class _ObservedAdmission(HistoryQueryAdmission):
    def __init__(self, verify: Callable[[], None]) -> None:
        super().__init__(capacity=1)
        self.verify = verify
        self.attempted = asyncio.Event()

    @asynccontextmanager
    async def admit(self) -> AsyncIterator[None]:
        self.verify()
        self.attempted.set()
        async with super().admit():
            yield


def _service(
    case: _HistoryCase,
    admission: HistoryQueryAdmission,
    *,
    user_id: int = 1,
) -> ConversationHistoryService:
    tracer, repository, channel = case
    return ConversationHistoryService(
        repository,
        user_id=user_id,
        tracer=tracer,
        history_queries=admission,
        conversation_channel=channel,
    )


async def _invoke(
    service: ConversationHistoryService,
    entry: _Entry,
) -> None:
    if entry == "history":
        detail = await service.get_detail(_THREAD)
        assert detail.task_trace is not None
    else:
        body = await service.follow_live(
            _THREAD,
            run_id=_RUN,
            last_event_id=None,
        )
        try:
            frame = await anext(body)
            payload = json.loads(frame.decode().split("data: ", 1)[1])
            assert payload["type"] == "snapshot"
            assert payload["snapshot"]["taskTrace"] is not None
        finally:
            await body.aclose()


async def _wait_for_signal(task: asyncio.Task[None], signal: asyncio.Event) -> None:
    waiter = asyncio.create_task(signal.wait())
    try:
        done, _ = await asyncio.wait(
            {task, waiter}, return_when=asyncio.FIRST_COMPLETED
        )
        if task in done:
            await task
            pytest.fail("请求在到达受控边界之前结束")
    finally:
        if not waiter.done():
            waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.parametrize("entry", ["history", "live"])
async def test_unauthorized_queries_do_not_enter_admission_or_read_trace(
    history_case: _HistoryCase,
    monkeypatch: pytest.MonkeyPatch,
    entry: _Entry,
) -> None:
    tracer, _, channel = history_case
    admission = _ObservedAdmission(
        lambda: pytest.fail("未授权请求不得占用初始查询容量")
    )
    read = AsyncMock(wraps=tracer.get)
    monkeypatch.setattr(tracer, "get", read)
    with pytest.raises(BusinessException) as caught:
        await _invoke(
            _service(history_case, admission, user_id=2),
            entry,
        )
    assert caught.value.error_code == ConversationErrorCode.NOT_FOUND
    read.assert_not_called()
    assert not admission.attempted.is_set()
    assert not channel.opened.is_set()


class _ControlledDeadline:
    """仅由测试信号触发取消，模拟超时上下文的异常归属"""

    def __init__(self) -> None:
        self.owner: asyncio.Task[None] | None = None
        self.triggered = False

    async def __aenter__(self) -> _ControlledDeadline:
        self.owner = asyncio.current_task()
        return self

    async def __aexit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del traceback
        if self.triggered and error_type is asyncio.CancelledError:
            assert self.owner is not None
            self.owner.uncancel()
            raise TimeoutError from error

    def expired(self) -> bool:
        return self.triggered

    def expire(self) -> None:
        assert self.owner is not None
        self.triggered = True
        self.owner.cancel()


@pytest.mark.parametrize("interruption", ["cancel", "timeout", "error"])
async def test_live_preflight_interruption_closes_open_replay_and_releases_capacity(
    history_case: _HistoryCase,
    monkeypatch: pytest.MonkeyPatch,
    interruption: Literal["cancel", "timeout", "error"],
) -> None:
    tracer, repository, channel = history_case
    deadline = _ControlledDeadline()
    monkeypatch.setattr(
        admission_module, "asyncio", SimpleNamespace(timeout=lambda _seconds: deadline)
    )
    admission = HistoryQueryAdmission(capacity=1)
    original_get_run = repository.get_run
    preflight_entered = asyncio.Event()
    hold_preflight = asyncio.Event()

    async def get_run(
        *, thread_pk: int, run_id: str
    ) -> ConversationRunRegistration | None:
        registration = await original_get_run(thread_pk=thread_pk, run_id=run_id)
        if channel.opened.is_set():
            preflight_entered.set()
            await hold_preflight.wait()
            if interruption == "error":
                return None
        return registration

    monkeypatch.setattr(repository, "get_run", get_run)
    task = asyncio.create_task(
        _invoke(
            _service(history_case, admission),
            "live",
        )
    )
    try:
        await _wait_for_signal(task, preflight_entered)
        assert channel.opened.is_set() and channel.closed_count == 0
        if interruption in {"timeout", "error"}:
            if interruption == "timeout":
                deadline.expire()
            else:
                hold_preflight.set()
            with pytest.raises(SystemException) as caught:
                await task
            assert caught.value.error_code == ConversationErrorCode.TRACE_UNAVAILABLE
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert channel.closed_count == 1
        monkeypatch.setattr(repository, "get_run", original_get_run)
        deadline = _ControlledDeadline()
        await _invoke(_service(history_case, admission), "live")
        assert channel.closed_count == 2
        snapshot = await tracer.store.snapshot(
            ThreadIdentity(namespace="ns_1", thread_id=_THREAD)
        )
        assert [writer.run_id for writer in snapshot.active_writers] == [_RUN]
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
