"""Execution observation is bounded, cancellable, and independent of execution control."""

from tinkerfin_automation.facade import Automation
from tinkerfin_automation.targets import ExecutionRequest


async def test_remote_worker_result_is_visible_through_shared_store(
    store_with_clock,
) -> None:
    store, clock = store_with_clock
    worker_app = Automation(namespace="shared", store=store, clock=clock)

    @worker_app.target("report")
    async def report(request: ExecutionRequest) -> dict[str, str]:
        return {"result": "ready"}

    async with worker_app.worker() as worker:
        client = Automation(namespace="shared", store=store, clock=clock)
        client.remote_target("report", execution_namespace="shared")
        async with client:
            run = await client.for_owner("subject").run("report")
            await worker.wait_until_idle()
            assert await run.wait() is run
            assert run.succeeded
            assert run.result == {"result": "ready"}
