"""Trace 权威列表摘要、Run 状态与恢复认领协调"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from ag_ui.core import BaseEvent

from tinkerfin import AgUiResumeReceipt, RunIdentity
from tinkerfin_messaging import AgUiChannel, RunNotFound, is_active_run_status
from tinkerfin_messaging.messaging import MessageChannel
from tinkerfin_studio.api.errors import ConversationErrorCode, SystemException
from tinkerfin_studio.infrastructure._failures import _cleanup_failure_priority
from tinkerfin_studio.infrastructure.database import Database
from tinkerfin_tracing import (
    Tracer,
    TraceRunNotFound,
    TraceThread,
    TraceThreadNotFound,
    TraceUpdate,
)

from .failures import FAILURE_PROJECTION, ConversationFailures
from .repository import ConversationRepository

_STALE_PREPARING_SECONDS = 30
_FOLLOW_RETRY_INITIAL_SECONDS = 0.05
_FOLLOW_RETRY_MAX_SECONDS = 2.0
_SUMMARY_REFRESH_TIMEOUT_SECONDS = 2.0
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _StalePreparingRun:
    """跨外部存活检查携带的无连接 Run 身份"""

    thread_pk: int
    run_pk: int
    identity: RunIdentity
    expected_updated_at: datetime


class ConversationTraceCoordinator:
    """把 Trace 当前事实收敛为 Studio 列表摘要，不保存会话正文"""

    def __init__(
        self,
        *,
        database: Database,
        tracer: Tracer,
        conversation_channel: MessageChannel[BaseEvent, BaseEvent] | AgUiChannel,
    ) -> None:
        self._database = database
        self._tracer = tracer
        self._conversation_channel = conversation_channel
        self._tasks: dict[tuple[int, str], asyncio.Task[None]] = {}
        self._close_task: asyncio.Task[list[BaseException]] | None = None
        self._closed = False

    def ensure(self, *, thread_pk: int, identity: RunIdentity) -> None:
        """为一个已注册 Run 保留唯一 Trace follow 所有者"""

        if self._closed:
            raise RuntimeError("Trace 摘要协调器已经关闭")
        key = (thread_pk, identity.run_id)
        current = self._tasks.get(key)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(
            self._follow(thread_pk=thread_pk, identity=identity),
            name=f"studio-trace-summary:{identity.run_id}",
        )
        task.add_done_callback(
            lambda completed, task_key=key: self._finished(task_key, completed)
        )
        self._tasks[key] = task

    async def reconcile(self, *, thread_pk: int, identity: RunIdentity) -> int:
        """读取固定 Trace 前缀并同步列表摘要"""

        return (
            await self._reconcile_view(thread_pk=thread_pk, identity=identity)
        ).as_of_seq

    async def _reconcile_view(
        self, *, thread_pk: int, identity: RunIdentity
    ) -> TraceThread:
        # 短事务只比较存储提供的顺序；相同时间精度内的冲突必须取得新观测后再结算
        try:
            async with asyncio.timeout(_SUMMARY_REFRESH_TIMEOUT_SECONDS):
                while True:
                    trace = await self._tracer.get(
                        identity.thread,
                        head_run_id=identity.run_id,
                        projections=(FAILURE_PROJECTION,),
                    )
                    result = await self._persist_view(thread_pk=thread_pk, view=trace)
                    if result == "applied":
                        return trace
                    if result == "generation_conflict":
                        raise SystemException(ConversationErrorCode.TRACE_UNAVAILABLE)
                    await asyncio.sleep(_FOLLOW_RETRY_INITIAL_SECONDS)
        except TimeoutError as error:
            raise SystemException(ConversationErrorCode.TRACE_UNAVAILABLE) from error

    async def settle_resume(
        self,
        *,
        thread_pk: int,
        receipt: AgUiResumeReceipt,
    ) -> None:
        """按框架已保存的恢复回执幂等结算业务认领"""

        async with self._database.session() as session:
            repository = ConversationRepository(session)
            await repository.settle_claims(
                thread_pk=thread_pk,
                receipt=receipt,
            )
            await repository.commit()
        await self.reconcile(thread_pk=thread_pk, identity=receipt.identity)

    async def recover_preparing(
        self,
        *,
        thread_pk: int | None = None,
    ) -> frozenset[int]:
        """清理没有 Trace 的过期 preparing 注册并恢复已有 Trace 摘要"""

        candidates: list[_StalePreparingRun] = []
        async with self._database.session() as session:
            repository = ConversationRepository(session)
            registrations = await repository.list_stale_unstarted_runs(
                older_than_seconds=_STALE_PREPARING_SECONDS,
                thread_pk=thread_pk,
            )
            recovered_threads: set[int] = set()
            for registration in registrations:
                thread = await repository.get_thread_by_pk(
                    registration.conversation_thread_id
                )
                if thread is None:
                    continue
                candidates.append(
                    _StalePreparingRun(
                        thread_pk=thread.id,
                        run_pk=registration.id,
                        identity=RunIdentity(
                            namespace=f"ns_{thread.user_id}",
                            thread_id=thread.thread_id,
                            run_id=registration.run_id,
                        ),
                        expected_updated_at=registration.updated_at,
                    )
                )
            # Trace 与 Redis 检查不占用 Studio 的业务连接或事务
            await repository.commit()

        recovered_threads: set[int] = set()
        deletions: list[_StalePreparingRun] = []
        for candidate in candidates:
            try:
                await self._tracer.get(
                    candidate.identity.thread,
                    head_run_id=candidate.identity.run_id,
                )
            except (TraceRunNotFound, TraceThreadNotFound):
                try:
                    producer_status = await self._conversation_channel.get_run_status(
                        identity=candidate.identity
                    )
                except RunNotFound:
                    producer_status = None
                # Messaging 只证明当前 owner 是否存活，终态不能替代 Trace 的 Agent 结果
                if is_active_run_status(producer_status):
                    continue
                deletions.append(candidate)
            else:
                recovered_threads.add(candidate.thread_pk)
                self.ensure(
                    thread_pk=candidate.thread_pk,
                    identity=candidate.identity,
                )

        if deletions:
            async with self._database.session() as session:
                repository = ConversationRepository(session)
                for candidate in deletions:
                    await repository.delete_unstarted_run(
                        thread_pk=candidate.thread_pk,
                        run_pk=candidate.run_pk,
                        run_id=candidate.identity.run_id,
                        delete_empty_thread=True,
                        expected_updated_at=candidate.expected_updated_at,
                        allow_starting=True,
                    )
                await repository.commit()
        return frozenset(recovered_threads)

    async def aclose(self) -> None:
        """等待全部摘要跟随结束；并发关闭共用清理，调用者取消在清理后传播

        Raises:
            RuntimeError: 跟随任务试图等待自己的关闭
            BaseException: 关闭调用者取消或跟随清理失败，多项失败共同保留
        """

        if asyncio.current_task() in self._tasks.values():
            raise RuntimeError("摘要跟随任务不能关闭自身协调器")
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(
                self._close_followers(tuple(self._tasks.values())),
                name="studio-trace-summary-close",
            )
        cancellation: asyncio.CancelledError | None = None
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError as error:
                cancellation = cancellation or error
        failures = self._close_task.result()
        failure: BaseException | None = None
        if failures:
            failure = (
                failures[0]
                if len(failures) == 1
                else BaseExceptionGroup("摘要跟随清理失败", failures)
            )
        if cancellation is not None:
            if failure is not None and _cleanup_failure_priority(failure) == 2:
                # 不重写原异常的 cause；组内保留进程控制、调用者取消和全部清理证据
                raise BaseExceptionGroup(
                    "摘要关闭与清理同时失败", [failure, cancellation]
                )
            raise cancellation from failure
        if failure is not None:
            raise failure

    @staticmethod
    async def _close_followers(
        tasks: tuple[asyncio.Task[None], ...],
    ) -> list[BaseException]:
        # 关闭只取消一次；取消之后的 finally 仍属于被等待的业务工作
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.wait(tasks)
        failures: list[BaseException] = []
        for task in tasks:
            try:
                task.result()
            except BaseException as error:  # noqa: BLE001 - 结算后由关闭调用者交付原始异常
                # gather 会重建 CancelledError；首次 result 才能保留取消携带的清理原因
                if _cleanup_failure_priority(error):
                    failures.append(error)
        return failures

    async def _follow(self, *, thread_pk: int, identity: RunIdentity) -> None:
        """从最新 Trace snapshot 恢复摘要并持续跟随到确定终态"""

        delay = _FOLLOW_RETRY_INITIAL_SECONDS
        while True:
            try:
                terminal = await self._follow_once(
                    thread_pk=thread_pk,
                    identity=identity,
                )
            except Exception as error:
                if self._closed:
                    # 关闭期间的清理错误必须交回关闭者，不能再开启一轮跟随
                    raise
                # 初始化尚未写入 Trace 是正常等待；其他读取或写入失败可从快照重建
                if not isinstance(error, (TraceRunNotFound, TraceThreadNotFound)):
                    logger.warning(
                        "Trace 摘要 follow 将重试: thread_pk=%s run_id=%s error_type=%s",
                        thread_pk,
                        identity.run_id,
                        type(error).__name__,
                    )
                terminal = False
            if terminal:
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, _FOLLOW_RETRY_MAX_SECONDS)

    async def _follow_once(
        self,
        *,
        thread_pk: int,
        identity: RunIdentity,
    ) -> bool:
        """从一个最新固定前缀恢复，并报告是否已经到达终态"""

        trace = await self._reconcile_view(thread_pk=thread_pk, identity=identity)
        if trace.summary.status.execution != "running":
            return True
        async with trace.follow() as updates:
            async for update in updates:
                result = await self._persist_view(thread_pk=thread_pk, view=update)
                if result != "applied":
                    # 迟到或不可比较的快照不能决定 follow 终止；下一轮重新获取权威观测
                    return False
                if update.summary.status.execution != "running":
                    return True
        return False

    async def _persist_view(
        self,
        *,
        thread_pk: int,
        view: TraceThread | TraceUpdate,
    ) -> Literal["applied", "stale", "ambiguous", "generation_conflict"]:
        """用同一字段映射与短事务接收权威快照或增量摘要"""

        summary = view.summary
        run_id = summary.status.head_run_id
        generation = (
            view.key.generation if isinstance(view, TraceThread) else view.generation
        )
        pending = summary.pending_interactions
        status, outcome = _summary_status(summary.status.execution, bool(pending))
        error_code = next(
            (
                item.error_code
                for item in ConversationFailures.model_validate(
                    view.projections[FAILURE_PROJECTION]
                ).failures
                if item.run_id == run_id
            ),
            None,
        )
        async with self._database.session() as session:
            repository = ConversationRepository(session)
            result = await repository.update_trace_summary(
                thread_pk=thread_pk,
                run_id=run_id,
                status=status,
                message_count=summary.message_count,
                tool_call_count=summary.tool_call_count,
                has_pending_interrupt=bool(pending),
                pending_interaction_kind=_pending_kind(item.kind for item in pending),
                terminal_outcome=outcome,
                error_code=error_code,
                updated_at=_database_time(summary.last_occurred_at),
                trace_generation=generation,
                trace_as_of_seq=view.as_of_seq,
                trace_observed_at=_database_time(view.observed_at),
            )
            if result == "applied" and outcome == "abandoned":
                await repository.cancel_claims(
                    thread_pk=thread_pk,
                    run_id=run_id,
                    resolution_id=f"trace:{generation}:{view.as_of_seq}",
                )
            await repository.commit()
        return result

    def _finished(
        self,
        key: tuple[int, str],
        task: asyncio.Task[None],
    ) -> None:
        if self._tasks.get(key) is task:
            self._tasks.pop(key, None)
        if task.cancelled():
            return
        task.exception()


def _summary_status(execution: str, pending: bool) -> tuple[str, str | None]:
    if execution == "running":
        return "running", None
    if execution == "waiting" or pending:
        return "waiting_approval", "interrupted"
    if execution == "succeeded":
        return "idle", "succeeded"
    if execution in {"cancelled", "abandoned"}:
        return "idle", execution
    if execution == "unknown":
        return "error", None
    return "error", "failed"


def _pending_kind(values: Iterable[str]) -> str | None:
    kinds = set(values)
    if not kinds:
        return None
    mapped = {
        "tinkerfin:plan_clarification": "plan_clarification",
        "tinkerfin:plan_review": "plan_review",
        "tool_approval": "tool_approval",
    }
    resolved = {mapped.get(kind, "input_required") for kind in kinds}
    return next(iter(resolved)) if len(resolved) == 1 else "input_required"


def _database_time(value: datetime) -> datetime:
    if value.utcoffset() is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


__all__ = ["ConversationTraceCoordinator"]
