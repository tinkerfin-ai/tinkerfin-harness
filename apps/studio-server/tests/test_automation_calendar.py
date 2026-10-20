"""周历聚合保持每日边界、分页与请求取消语义"""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_automation_integration import automation_environment, automation_resources

from tinkerfin_automation import Automation
from tinkerfin_automation.clock import ManualClock
from tinkerfin_studio.api.dependencies import get_user_context
from tinkerfin_studio.application import create_application
from tinkerfin_studio.auth.types import UserContext
from tinkerfin_studio.automation.service import StudioAutomationService

__all__ = ["automation_environment", "automation_resources"]


async def test_calendar_dates_cursors_filters_and_project_ownership(
    automation_resources, monkeypatch
):
    clock = ManualClock(datetime(2030, 1, 6, 15, 59, tzinfo=UTC))
    automation = Automation(clock=clock)
    automation.remote_target("job", execution_namespace="calendar")
    monkeypatch.setattr(automation_resources, "automation", automation)
    application = create_application(lifespan=None)
    application.state.resources = automation_resources
    user = UserContext(user_id=1, username="owner", roles=(), disabled=False)
    application.dependency_overrides[get_user_context] = lambda: user
    async with (
        automation,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://test",
            params={"projectId": "project-1"},
        ) as client,
    ):
        owner = automation.for_owner("1:project-1")
        await owner.run("job")
        await clock.advance(timedelta(minutes=1))
        first = await owner.run("job")
        second = await owner.run("job")
        await automation.for_owner("2:project-2").run("job")
        await clock.advance(timedelta(days=6))
        last = await owner.run("job")
        await clock.advance(timedelta(days=1))
        await owner.run("job")
        response = await client.get(
            "/api/automation/runs/calendar",
            params={
                "weekStart": "2030-01-07",
                "limit": 1,
            },
        )
        assert response.status_code == 200, response.text
        days = response.json()["data"]["days"]
        assert [day["date"] for day in days] == [
            f"2030-01-{day:02}" for day in range(7, 14)
        ]
        assert {item["id"] for item in days[0]["items"]} <= {first.id, second.id}
        assert len(days[0]["items"]) == 1
        assert all(
            day["items"] == [] and day["nextCursor"] is None for day in days[1:6]
        )
        assert [item["id"] for item in days[6]["items"]] == [last.id]
        more = await client.get(
            "/api/automation/runs",
            params={
                "from": "2030-01-07T00:00:00+08:00",
                "until": "2030-01-08T00:00:00+08:00",
                "cursor": days[0]["nextCursor"],
                "limit": 1,
            },
        )
        assert more.status_code == 200, more.text
        assert {
            item["id"] for item in days[0]["items"] + more.json()["data"]["items"]
        } == {first.id, second.id}
        filtered = await client.get(
            "/api/automation/runs/calendar",
            params={
                "weekStart": "2030-01-07",
                "status": "succeeded",
                "query": "日报",
            },
        )
        assert all(day["items"] == [] for day in filtered.json()["data"]["days"])
        for params in (
            {"weekStart": "bad"},
            {"weekStart": "2030-01-07", "limit": 101},
            {"weekStart": "9999-12-31"},
        ):
            assert (
                await client.get("/api/automation/runs/calendar", params=params)
            ).status_code in (400, 422)
        denied = await client.get(
            "/api/automation/runs/calendar",
            params={"projectId": "project-2", "weekStart": "2030-01-07"},
        )
        assert denied.status_code == 404


@pytest.mark.parametrize("cancel", [False, True])
async def test_calendar_failure_and_cancellation_close_all_reads(
    automation_resources, monkeypatch, cancel
):
    started = set()
    closed = set()
    all_started = asyncio.Event()

    async def read_day(self, *, queued_from, **kwargs):
        day = queued_from.day
        started.add(day)
        if len(started) == 7:
            all_started.set()
        try:
            await all_started.wait()
            if day == 7 and not cancel:
                raise ValueError("invalid calendar filter")
            await asyncio.Event().wait()
        finally:
            closed.add(day)

    monkeypatch.setattr(StudioAutomationService, "list_runs", read_day)
    application = create_application(lifespan=None)
    application.state.resources = automation_resources
    application.dependency_overrides[get_user_context] = lambda: UserContext(
        user_id=1,
        username="owner",
        roles=(),
        disabled=False,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        request = asyncio.create_task(
            client.get(
                "/api/automation/runs/calendar",
                params={
                    "projectId": "project-1",
                    "weekStart": "2030-01-07",
                },
            )
        )
        try:
            await all_started.wait()
            if cancel:
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
            else:
                assert (await request).status_code == 422
            assert closed == set(range(7, 14))
        finally:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
