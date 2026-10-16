"""模型写命令自动结算事务，HTTP 调用方无需额外取消保护"""

import asyncio
from unittest.mock import create_autospec

import pytest

from tinkerfin_studio.models.repository import AgentModelRepository
from tinkerfin_studio.models.schemas import AgentModelSave
from tinkerfin_studio.models.service import AgentModelService


@pytest.mark.parametrize("operation", ["save", "default", "delete"])
@pytest.mark.parametrize("rollback_fails", [False, True])
async def test_model_write_waits_for_rollback_after_repeated_cancellation(
    operation: str, rollback_fails: bool
) -> None:
    entered, rolling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    finished = False
    failure = OSError("回滚失败")

    async def lock() -> None:
        entered.set()
        await asyncio.Event().wait()

    async def rollback() -> None:
        nonlocal finished
        rolling.set()
        await release.wait()
        finished = True
        if rollback_fails:
            raise failure

    repository = create_autospec(AgentModelRepository, instance=True)
    repository.lock_owner.side_effect = lock
    repository.rollback.side_effect = rollback
    service = AgentModelService(repository)
    if operation == "save":
        command = service.save_settings(
            AgentModelSave(
                connection_id="configured",
                model_id="model",
                display_name="模型",
                model_name="model",
            )
        )
    elif operation == "default":
        command = service.set_default("model")
    else:
        command = service.delete_settings("model")
    task = asyncio.create_task(command)
    await entered.wait()
    task.cancel("首次取消")
    await rolling.wait()
    task.cancel("重复取消")
    delivered = asyncio.Event()
    asyncio.get_running_loop().call_soon(delivered.set)
    await delivered.wait()
    premature = task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await task
    assert not premature
    assert finished
    repository.rollback.assert_awaited_once()
    if rollback_fails:
        pending: list[BaseException] = [cancelled.value]
        seen: set[int] = set()
        while pending:
            error = pending.pop()
            if id(error) in seen:
                continue
            seen.add(id(error))
            pending.extend(
                cause
                for cause in (error.__cause__, error.__context__)
                if cause is not None
            )
            if isinstance(error, BaseExceptionGroup):
                pending.extend(error.exceptions)
        assert id(failure) in seen
