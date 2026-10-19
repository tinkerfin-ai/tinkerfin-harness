"""Keep one delivery owner while storage admission is pending."""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from tinkerfin_remote import RemoteError, RemoteErrorCode, RemoteTools


@pytest.mark.parametrize("cancel", [False, True])
async def test_concurrent_delivery_is_rejected_while_first_waits_for_storage(
    tmp_path, cancel
):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'remote.sqlite'}",
        pool_size=1,
        max_overflow=0,
    )
    try:
        async with RemoteTools(engine) as remote:
            endpoint = remote.endpoint(namespace="account", target="device")

            async def send(_frame: str) -> None:
                pass

            async def receive() -> str:
                raise ConnectionError("Owned test transport disconnected")

            async def attach(entered: asyncio.Event) -> Exception:
                entered.set()
                try:
                    await endpoint.serve(send=send, receive=receive)
                except (ConnectionError, RemoteError) as error:
                    return error
                raise AssertionError("The test transport must disconnect")

            assert isinstance(await attach(asyncio.Event()), ConnectionError)
            async with engine.connect():
                first_entered, second_entered = asyncio.Event(), asyncio.Event()
                first = asyncio.create_task(attach(first_entered))
                await first_entered.wait()
                second = asyncio.create_task(attach(second_entered))
                await second_entered.wait()
                if cancel:
                    first.cancel()
            first_error, second_error = await asyncio.gather(
                first, second, return_exceptions=True
            )
            assert isinstance(
                first_error, asyncio.CancelledError if cancel else ConnectionError
            )
            assert isinstance(second_error, RemoteError)
            assert second_error.code == RemoteErrorCode.CONFLICT
            assert isinstance(await attach(asyncio.Event()), ConnectionError)
    finally:
        await engine.dispose()
