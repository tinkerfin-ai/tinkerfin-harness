"""A reopened Redis saver resumes the original reviewed delegated attempt."""

from __future__ import annotations

from uuid import uuid4

import pytest
from ag_ui.core import RunFinishedEvent
from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from redis.asyncio import Redis
from test_durable_delegation import _ReviewCase

from tinkerfin import AgUiResumeRequest
from tinkerfin.checkpoints import delete_thread
from tinkerfin_contracts import ThreadIdentity

pytestmark = [pytest.mark.docker_integration, pytest.mark.redis_e2e]


async def test_reopened_redis_saver_resumes_exact_delegation_and_keeps_private_results(
    redis_checkpoint_url: str,
) -> None:
    token = uuid4().hex
    checkpoint_prefix = f"test:durable:{token}:checkpoint"
    write_prefix = f"test:durable:{token}:write"
    first_client = Redis.from_url(redis_checkpoint_url, decode_responses=False)
    second_client = Redis.from_url(redis_checkpoint_url, decode_responses=False)
    first = AsyncRedisSaver(
        redis_client=first_client,
        checkpoint_prefix=checkpoint_prefix,
        checkpoint_write_prefix=write_prefix,
    )
    second = AsyncRedisSaver(
        redis_client=second_client,
        checkpoint_prefix=checkpoint_prefix,
        checkpoint_write_prefix=write_prefix,
    )
    try:
        await first.asetup()
        case = _ReviewCase(first)
        interrupt_id = await case.pause()
        await first_client.aclose()
        await second.asetup()
        case.saver = second
        stream = case.build().open_agui_run(
            thread_id="thread",
            run_id="resume",
            parent_run_id="request",
            resume=AgUiResumeRequest.model_validate(
                {
                    "entries": [
                        {
                            "interruptId": interrupt_id,
                            "status": "resolved",
                            "payload": {"type": "approve"},
                        }
                    ]
                }
            ),
        )
        events = [event async for event in stream]
        assert stream.error is None and isinstance(events[-1], RunFinishedEvent)
        assert case.executed == ["approved"] and case.child.attempts == 3
        assert len(case.decisions) == 1
        assert all("record_digest" not in event.model_dump_json() for event in events)
    finally:
        try:
            await delete_thread(
                second, thread=ThreadIdentity(namespace="durable", thread_id="thread")
            )
            for index in (second.checkpoints_index, second.checkpoint_writes_index):
                await second_client.execute_command(
                    "FT.DROPINDEX", index.schema.index.name, "DD"
                )
        finally:
            await first_client.aclose()
            await second_client.aclose()
