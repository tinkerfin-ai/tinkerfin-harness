from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from types import SimpleNamespace, TracebackType

import pytest
from httpx import ASGITransport, AsyncClient

from tinkerfin_studio.application import create_application
from tinkerfin_studio.resources import (
    _enter_lifespan_context,
    _LifespanOutcome,
    _settle_lifespan_stack,
)


class _RecordingResource(AbstractAsyncContextManager["_RecordingResource"]):
    """记录资源退出时收到的生命周期主异常"""

    def __init__(
        self,
        name: str,
        exits: list[tuple[str, BaseException | None]],
        *,
        enter_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self._name = name
        self._exits = exits
        self._enter_error = enter_error
        self._close_error = close_error

    async def __aenter__(self) -> _RecordingResource:
        if self._enter_error is not None:
            raise self._enter_error
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del exc_type, traceback
        self._exits.append((self._name, exc_value))
        if self._close_error is not None:
            raise self._close_error
        return False


async def test_lifespan_cleanup_preserves_cancellation_for_every_resource() -> None:
    """清理失败不能替换主体取消，且后续资源仍收到同一主因"""

    exits: list[tuple[str, BaseException | None]] = []
    stack = AsyncExitStack()
    outcome = _LifespanOutcome()
    cleanup_error = RuntimeError("close failed")
    await _enter_lifespan_context(stack, outcome, _RecordingResource("first", exits))
    await _enter_lifespan_context(
        stack,
        outcome,
        _RecordingResource("second", exits, close_error=cleanup_error),
    )
    primary = asyncio.CancelledError()
    outcome.capture(primary)

    with pytest.raises(asyncio.CancelledError) as raised:
        await _settle_lifespan_stack(stack, outcome)

    assert raised.value is primary
    assert raised.value.__cause__ is cleanup_error
    assert exits == [("second", primary), ("first", primary)]


async def test_lifespan_cleanup_process_control_precedes_an_ordinary_failure() -> None:
    """资源关闭的进程控制异常不能被普通业务失败转换"""

    exits: list[tuple[str, BaseException | None]] = []
    stack = AsyncExitStack()
    outcome = _LifespanOutcome()
    cleanup_control = SystemExit(7)
    await _enter_lifespan_context(
        stack,
        outcome,
        _RecordingResource("resource", exits, close_error=cleanup_control),
    )
    primary = RuntimeError("body failed")
    outcome.capture(primary)

    with pytest.raises(SystemExit) as raised:
        await _settle_lifespan_stack(stack, outcome)

    assert raised.value is cleanup_control
    assert raised.value.__cause__ is primary
    assert exits == [("resource", primary)]


async def test_lifespan_partial_startup_closes_entered_resources_with_root_cause() -> (
    None
):
    """部分启动失败必须关闭已进入资源并保持初始化主因"""

    exits: list[tuple[str, BaseException | None]] = []
    stack = AsyncExitStack()
    outcome = _LifespanOutcome()
    await _enter_lifespan_context(stack, outcome, _RecordingResource("ready", exits))
    primary = RuntimeError("startup failed")
    try:
        await _enter_lifespan_context(
            stack,
            outcome,
            _RecordingResource("broken", exits, enter_error=primary),
        )
    except RuntimeError as error:
        outcome.capture(error)

    with pytest.raises(RuntimeError) as raised:
        await _settle_lifespan_stack(stack, outcome)

    assert raised.value is primary
    assert exits == [("ready", primary)]


async def test_lifespan_attempts_every_close_before_restoring_body_failure() -> None:
    """多个关闭失败也必须完成全部回调并恢复业务主因"""

    exits: list[tuple[str, BaseException | None]] = []
    stack = AsyncExitStack()
    outcome = _LifespanOutcome()
    await _enter_lifespan_context(
        stack,
        outcome,
        _RecordingResource("first", exits, close_error=RuntimeError("first close")),
    )
    await _enter_lifespan_context(
        stack,
        outcome,
        _RecordingResource("second", exits, close_error=RuntimeError("second close")),
    )
    primary = ValueError("body failed")
    outcome.capture(primary)

    with pytest.raises(ValueError) as raised:
        await _settle_lifespan_stack(stack, outcome)

    assert raised.value is primary
    assert [name for name, error in exits if error is primary] == ["second", "first"]
    assert isinstance(raised.value.__cause__, RuntimeError)


async def test_application_exposes_liveness_without_external_resources() -> None:
    """liveness 只表达进程存活，不触发外部连接"""

    application = create_application(lifespan=None)

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_requires_initialized_resources() -> None:
    """资源尚未初始化时 readiness 必须返回 503"""

    application = create_application(lifespan=None)

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "components": {}}


async def test_readiness_reports_dependency_status_without_error_details() -> None:
    """readiness 只公开稳定组件状态，不泄漏底层异常"""

    class FakeReadiness:
        async def check(self) -> dict[str, bool]:
            return {
                "business_database": True,
                "components_database": True,
                "redis": True,
                "opensandbox": False,
            }

    application = create_application(lifespan=None)
    application.state.resources = SimpleNamespace(readiness=FakeReadiness())

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "components": {
            "business_database": True,
            "components_database": True,
            "redis": True,
            "opensandbox": False,
        },
    }


async def test_unknown_route_uses_the_shared_error_envelope() -> None:
    """未匹配路由不得泄漏 FastAPI 默认错误结构"""

    application = create_application(lifespan=None)

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get("/missing")

    assert response.status_code == 404
    assert response.json() == {
        "code": 404,
        "message": "请求未找到",
        "data": None,
    }
