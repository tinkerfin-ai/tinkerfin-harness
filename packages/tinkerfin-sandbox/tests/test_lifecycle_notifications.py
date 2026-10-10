"""Confirmed lifecycle facts, bounded observer delivery, and recovery interaction."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_manager import (
    _FakeClient,
    _new_manager,
)
from tests.support.sql_engines import SqlEngineFactory

from tinkerfin_sandbox import (
    OpenSandboxWarmPoolUnavailableError,
    SQLAlchemyOpenSandboxState,
)


@pytest.fixture
def startup_state_url(tmp_path: Path) -> str:
    """Resolve the isolated SQLite database before the asynchronous test starts."""
    return f"sqlite+aiosqlite:///{tmp_path / 'startup-claim.db'}"


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize(
    "startup_state_url",
    ["sqlite"],
    indirect=True,
)
async def test_new_manager_cannot_accept_unverified_capacity_claimed_by_a_peer(
    sql_engine: SqlEngineFactory, startup_state_url: str, strict: bool
) -> None:
    url = startup_state_url
    peer = SQLAlchemyOpenSandboxState(engine=sql_engine(url), namespace="startup")
    await peer.start(warm_pool_size=1)
    creating = await peer.claim_warm_slot()
    assert creating is not None
    await peer.publish_warm(creating, "unverified")
    checking = await peer.claim_ready_warm_slot(exclude_slots=())
    assert checking is not None
    client = _FakeClient()
    manager = _new_manager(
        client=client,
        state=SQLAlchemyOpenSandboxState(engine=sql_engine(url), namespace="startup"),
        warm_pool_size=1,
        fail_on_startup_warmup_error=strict,
    )
    try:
        if strict:
            with pytest.raises(OpenSandboxWarmPoolUnavailableError):
                await manager.start()
        else:
            await manager.start()
        assert await peer.warm_pool_ready()
        with pytest.raises(OpenSandboxWarmPoolUnavailableError):
            await manager.check_ready()
        assert client.connect_calls == [] and client.create_calls == 0
    finally:
        await peer.release_warm(checking)
        await manager.aclose()
        await peer.aclose()
