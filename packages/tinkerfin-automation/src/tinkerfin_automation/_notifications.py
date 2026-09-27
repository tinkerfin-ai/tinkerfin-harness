"""Publish source-owned routing facts only after an Automation mutation commits."""

from __future__ import annotations

import logging

from pydantic import JsonValue

from tinkerfin_notifications import (
    Notification,
    NotificationError,
    Notifications,
    NotificationScope,
)

from .models import AutomationExecution, AutomationTask

logger = logging.getLogger("tinkerfin.automation.notifications")


class _ChangedResources:
    """Coalesce only the current operation's write set, never retained history.

    Stores record these facts within their mutation boundary and publish after all
    database and ownership locks are released. The write set bounds transient
    entries; workers bound claims and cleanup scans independently of this collector.
    """

    def __init__(self) -> None:
        self._changes: dict[tuple[NotificationScope, str, str], Notification] = {}

    def task(self, task: AutomationTask) -> None:
        self._add(
            Notification(
                scope=NotificationScope(task.namespace, task.owner_id),
                topic="automation.task.changed",
                key=task.task_id,
            )
        )

    def execution(self, execution: AutomationExecution) -> None:
        details: dict[str, JsonValue] = {
            "task_id": execution.task_id,
            "runtime_namespace": execution.identity.namespace,
            "thread_id": execution.identity.thread_id,
            "run_id": execution.identity.run_id,
        }
        self._add(
            Notification(
                scope=NotificationScope(execution.namespace, execution.owner_id),
                topic="automation.execution.changed",
                key=execution.execution_id,
                details=details,
            )
        )

    def _add(self, change: Notification) -> None:
        self._changes[(change.scope, change.topic, change.key)] = change

    async def publish(self, notifications: Notifications | None) -> None:
        if notifications is None:
            return
        for change in self._changes.values():
            try:
                await notifications.publish(change)
            except NotificationError as error:
                # The mutation is already committed. Consumers repair lost hints
                # with authoritative reads; a failed hint cannot undo that mutation.
                try:
                    logger.warning(
                        "Automation change notification could not be published",
                        extra={"tinkerfin_code": error.code.value},
                    )
                except Exception:  # noqa: BLE001 - host logging cannot affect committed state
                    pass


__all__ = ["_ChangedResources"]
