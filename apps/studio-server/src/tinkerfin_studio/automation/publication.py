"""自动化权威结算后开放普通会话入口"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, timedelta

from tinkerfin_automation import Automation, AutomationWaitTimeout, ExecutionStatus
from tinkerfin_automation.clock import AutomationClock, SystemClock
from tinkerfin_notifications import Notifications
from tinkerfin_studio.changes import notify_change
from tinkerfin_studio.conversation.repository import (
    ConversationRepository,
    UnpublishedAutomationConversation,
)
from tinkerfin_studio.infrastructure._failures import _cleanup_failure_priority
from tinkerfin_studio.infrastructure.database import Database

from .ownership import automation_owner

logger = logging.getLogger(__name__)
_CAPACITY = 16
_PAGE_SIZE = 100
_REPAIR_INTERVAL = timedelta(seconds=30)
_OBSERVATION_SECONDS = 30.0


class AutomationConversationPublications:
    """拥有有界入口发布任务，未完成工作保留在业务数据库

    借用独立查询客户端，只用公开等待接口观察执行，不启动或取消自动化。
    worker 与 Trace 先结算，再关闭本对象；查询客户端、通知和数据库最后释放。
    """

    def __init__(
        self,
        *,
        database: Database,
        automation: Automation,
        notifications: Notifications,
        clock: AutomationClock | None = None,
    ) -> None:
        self._database = database
        self._automation = automation
        self._notifications = notifications
        self._clock = clock or SystemClock()
        self._wake = asyncio.Event()
        self._supervisor: asyncio.Task[None] | None = None
        self._observations: dict[int, asyncio.Task[bool]] = {}
        self._close_task: asyncio.Task[list[BaseException]] | None = None
        self._closed = False
        self._cursor = 0

    async def start(self) -> None:
        """启动时扫描持久待处理记录，结构检查失败不隐藏在后台"""
        if self._closed or self._supervisor is not None:
            raise RuntimeError("会话发布服务不能重复启动")
        await self._fill()
        if self._closed:
            raise RuntimeError("会话发布服务已经关闭")
        self._supervisor = asyncio.create_task(
            self._follow(), name="studio-automation-publications"
        )

    def wake(self) -> None:
        """合并已受理登记的唤醒；关闭后的新工作由下一实例恢复"""
        if not self._closed:
            self._wake.set()

    async def _candidates(
        self, *, after: int = 0
    ) -> tuple[UnpublishedAutomationConversation, ...]:
        async with self._database.session() as session:
            return await ConversationRepository(session).unpublished_automations(
                after=after, limit=_PAGE_SIZE
            )

    async def _fill(self) -> None:
        if self._closed or len(self._observations) >= _CAPACITY:
            return
        candidates = await self._candidates(after=self._cursor)
        if not candidates and self._cursor:
            self._cursor = 0
            candidates = await self._candidates()
        if self._closed:
            return
        for candidate in candidates:
            if len(self._observations) >= _CAPACITY:
                break
            self._cursor = candidate.thread_pk
            if candidate.thread_pk in self._observations:
                continue
            task = asyncio.create_task(
                self._observe(candidate),
                name=f"studio-automation-publication:{candidate.execution_id}",
            )
            self._observations[candidate.thread_pk] = task
            task.add_done_callback(
                lambda completed, key=candidate.thread_pk: self._finished(
                    key, completed
                )
            )

    def _finished(self, key: int, task: asyncio.Task[bool]) -> None:
        if task.cancelled():
            return
        # 失败槽位保留到下一补偿周期，其他执行完成不能触发它的无间隔重试
        if task.exception() is None and task.result():
            self._observations.pop(key, None)
            self.wake()

    async def _follow(self) -> None:
        deadline = self._clock.now() + _REPAIR_INTERVAL
        while True:
            self._wake.clear()
            if self._clock.now() >= deadline:
                for key, task in tuple(self._observations.items()):
                    if task.done():
                        if not task.cancelled():
                            task.result()
                        self._observations.pop(key, None)
                deadline = self._clock.now() + _REPAIR_INTERVAL
            try:
                await self._fill()
            except Exception as error:
                if self._closed:
                    raise
                logger.warning("自动化会话发布扫描失败 reason=%s", type(error).__name__)
            wake = asyncio.create_task(self._wake.wait(), name="publication-wakeup")
            repair = asyncio.create_task(
                self._clock.wait_until(deadline), name="publication-repair"
            )
            failures: list[BaseException] = []
            try:
                await asyncio.wait((wake, repair), return_when=asyncio.FIRST_COMPLETED)
            except BaseException as error:  # noqa: BLE001 - 子等待清理后一起交付原始失败
                failures.append(error)
            finally:
                for task in (wake, repair):
                    if not task.done():
                        task.cancel()
                await asyncio.wait((wake, repair))
                for task in (wake, repair):
                    try:
                        task.result()
                    except BaseException as error:  # noqa: BLE001 - 取消也可能携带清理失败
                        if _cleanup_failure_priority(error):
                            failures.append(error)
            if failures:
                raise (
                    failures[0]
                    if len(failures) == 1
                    else BaseExceptionGroup("会话发布等待与清理失败", failures)
                )

    async def _observe(self, candidate: UnpublishedAutomationConversation) -> bool:
        try:
            run = await self._automation.for_owner(
                automation_owner(candidate.user_id, candidate.project_id)
            ).get_run(candidate.execution_id)
            if run.identity != candidate.identity:
                raise ValueError("会话与自动化执行身份不一致")
            await run.wait(timeout=_OBSERVATION_SECONDS)
            if (
                not run.status.is_terminal
                and run.status != ExecutionStatus.NEEDS_ATTENTION
            ):
                return False
            await self._publish(candidate)
            return True
        except AutomationWaitTimeout:
            return False
        except Exception as error:
            if self._closed:
                raise
            logger.warning(
                "自动化会话尚未开放 execution=%s reason=%s",
                candidate.execution_id,
                type(error).__name__,
            )
            return False

    async def _publish(self, candidate: UnpublishedAutomationConversation) -> None:
        async with self._database.session() as session:
            repository = ConversationRepository(session)
            changed = await repository.publish_automation(
                candidate, at=self._clock.now().astimezone(UTC).replace(tzinfo=None)
            )
            await repository.commit()
        if changed:
            await notify_change(
                self._notifications,
                user_id=candidate.user_id,
                topic="studio.conversation.changed",
                key=candidate.identity.thread_id,
                details={
                    "automation_execution_id": candidate.execution_id,
                    "automation_project_id": candidate.project_id,
                },
            )

    async def _close(self) -> list[BaseException]:
        self._closed = True
        owned: list[asyncio.Task[bool | None]] = [*self._observations.values()]
        if self._supervisor is not None:
            owned.append(self._supervisor)
        for task in owned:
            if not task.done():
                task.cancel()
        if owned:
            await asyncio.wait(owned)
        failures: list[BaseException] = []
        for task in owned:
            try:
                task.result()
            except BaseException as error:  # noqa: BLE001 - 原始取消可能携带订阅清理失败
                if _cleanup_failure_priority(error):
                    failures.append(error)
        self._observations.clear()
        # 停服只做一页、有限时间的权威补读；远端活跃执行留待下次启动
        try:
            async with asyncio.timeout(5):
                for candidate in await self._candidates():
                    run = await self._automation.for_owner(
                        automation_owner(candidate.user_id, candidate.project_id)
                    ).get_run(candidate.execution_id)
                    if run.identity != candidate.identity:
                        raise ValueError("会话与自动化执行身份不一致")
                    if (
                        run.status.is_terminal
                        or run.status == ExecutionStatus.NEEDS_ATTENTION
                    ):
                        await self._publish(candidate)
        except BaseException as error:  # noqa: BLE001 - 关闭调用者统一交付取消与全部失败
            logger.warning("自动化会话关闭校准未完成 reason=%s", type(error).__name__)
            failures.append(error)
        return failures

    async def aclose(self) -> None:
        """等待观察清理与最后校准，再传播调用者取消；重复关闭等待同一任务"""
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._close(), name="studio-automation-publications-close"
            )
        cancellation: asyncio.CancelledError | None = None
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError as error:
                cancellation = cancellation or error
        failures = self._close_task.result()
        failure = (
            failures[0]
            if len(failures) == 1
            else BaseExceptionGroup("自动化会话发布任务异常", failures)
            if failures
            else None
        )
        if cancellation is not None:
            if failure is not None and _cleanup_failure_priority(failure) == 2:
                raise BaseExceptionGroup(
                    "会话发布关闭取消与失败", [failure, cancellation]
                )
            raise cancellation from failure
        if failure is not None:
            raise failure
