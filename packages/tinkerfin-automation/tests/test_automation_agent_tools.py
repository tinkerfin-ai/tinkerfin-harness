from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tinkerfin_automation import (
    Automation,
    ExecutionRequest,
    create_automation_tools,
)
from tinkerfin_automation.clock import ManualClock


@pytest.mark.asyncio
async def test_agent_tools_bind_identity_and_target_permissions() -> None:
    clock = ManualClock(datetime(2026, 9, 9, 8, tzinfo=UTC))
    automation = Automation(namespace="app", clock=clock)

    @automation.target("summary")
    async def summary(request: ExecutionRequest) -> None:
        return None

    async with automation.worker():
        tools = create_automation_tools(
            automation.for_owner("owner-1"),
            allowed_targets={"summary"},
        )
        by_name = {tool.name: tool for tool in tools}
        assert set(by_name) == {
            "create_automation",
            "update_automation",
            "pause_automation",
            "enable_automation",
            "delete_automation",
            "get_automation",
            "list_automations",
            "execute_automation_once",
            "run_automation_task_now",
        }
        for tool in tools:
            properties = tool.args
            assert "owner_id" not in properties
            assert "namespace" not in properties
            assert "credentials" not in properties
            assert "code" not in properties

        created = await by_name["create_automation"].ainvoke(
            {
                "name": "Summary",
                "schedule": {
                    "kind": "interval",
                    "every_seconds": 60,
                    "start_at": "2026-09-09T08:00:00Z",
                },
                "target": "summary",
                "input": {"project_id": "project-1"},
                "request_id": "agent-create-1",
            }
        )
        assert created["name"] == "Summary"
        listed = await by_name["list_automations"].ainvoke({})
        assert listed["items"][0]["task_id"] == created["task_id"]
        executed_once = await by_name["execute_automation_once"].ainvoke(
            {
                "target": "summary",
                "input": {"project_id": "project-2"},
                "request_id": "agent-once-1",
            }
        )
        assert executed_once["task_id"] is None
        task_execution = await by_name["run_automation_task_now"].ainvoke(
            {"task_id": created["task_id"], "request_id": "agent-task-now-1"}
        )
        assert task_execution["task_id"] == created["task_id"]
        assert "task_id" not in by_name["execute_automation_once"].args
        assert "target" not in by_name["run_automation_task_now"].args

        with pytest.raises(ValueError, match="not allowed"):
            await by_name["create_automation"].ainvoke(
                {
                    "name": "Unsafe",
                    "schedule": {
                        "kind": "interval",
                        "every_seconds": 60,
                        "start_at": "2026-09-09T08:00:00Z",
                    },
                    "target": "arbitrary-python",
                    "request_id": "agent-create-2",
                }
            )
        with pytest.raises(ValueError, match="not allowed"):
            await by_name["execute_automation_once"].ainvoke(
                {
                    "target": "arbitrary-python",
                    "input": {},
                    "request_id": "agent-once-2",
                }
            )
