"""会话归属、Run 注册、恢复认领与列表摘要数据访问"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from ag_ui.core.types import ResumeEntry
from pydantic import JsonValue
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Update

from tinkerfin_studio.agent.access import AccessMode
from tinkerfin_studio.conversation.models import (
    ConversationInterruptClaim,
    ConversationRunRegistration,
    ConversationThread,
)


@dataclass(frozen=True, slots=True)
class UnstartedRunCleanup:
    """描述未启动 Run 清理是否删除注册和空会话"""

    run_deleted: bool
    thread_deleted: bool


class ConversationRepository:
    """在调用方事务内维护 Studio 自有会话数据"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_thread(
        self,
        *,
        user_id: int,
        thread_id: str,
        title: str,
        model_id: str | None,
    ) -> ConversationThread:
        """创建不含正文的用户会话记录"""

        now = datetime.now(UTC).replace(tzinfo=None)
        entity = ConversationThread(
            user_id=user_id,
            thread_id=thread_id,
            title=title,
            status="idle",
            last_run_id=None,
            last_model=model_id,
            message_count=0,
            tool_call_count=0,
            has_pending_interrupt=False,
            pending_interaction_kind=None,
            pinned=False,
            created_at=now,
            updated_at=now,
            deleted_at=None,
        )
        self._session.add(entity)
        await self._session.flush()
        return entity

    async def create_run_registration(
        self,
        *,
        thread_id: int,
        run_id: str,
        parent_run_id: str | None,
        model_id: str,
        input_json: dict[str, JsonValue],
        access_mode: AccessMode = "full",
    ) -> ConversationRunRegistration:
        """创建固定模型与请求快照的主 Run 注册"""

        now = datetime.now(UTC).replace(tzinfo=None)
        entity = ConversationRunRegistration(
            conversation_thread_id=thread_id,
            run_id=run_id,
            parent_run_id=parent_run_id,
            model_id=model_id,
            access_mode=access_mode,
            status="preparing",
            input_json=input_json,
            terminal_outcome=None,
            error_code=None,
            started_at=now,
            finished_at=None,
            created_at=now,
            updated_at=now,
        )
        self._session.add(entity)
        await self._session.flush()
        return entity

    async def activate_run_registration(
        self,
        *,
        thread_pk: int,
        run_pk: int,
        run_id: str,
    ) -> bool:
        """在框架 Run 可查询后以 CAS 发布业务 head"""

        thread = await self.lock_thread(thread_pk)
        if thread is None or thread.status == "deleting":
            return False
        run = await self.get_run_for_update(
            thread_pk=thread_pk,
            run_id=run_id,
        )
        if run is None or run.id != run_pk or run.status != "preparing":
            return False
        now = datetime.now(UTC).replace(tzinfo=None)
        run.status = "starting"
        run.updated_at = now
        thread.last_run_id = run_id
        thread.last_model = run.model_id
        thread.last_access_mode = run.access_mode
        thread.status = "running"
        thread.updated_at = now
        await self._session.flush()
        return True

    async def get_thread_by_pk(self, thread_pk: int) -> ConversationThread | None:
        """按内部主键读取会话"""

        return await self._session.get(ConversationThread, thread_pk)

    async def get_thread(
        self,
        *,
        user_id: int,
        thread_id: str,
    ) -> ConversationThread | None:
        """按用户归属读取未删除会话"""

        return await self._session.scalar(
            select(ConversationThread).where(
                ConversationThread.user_id == user_id,
                ConversationThread.thread_id == thread_id,
                ConversationThread.deleted_at.is_(None),
            )
        )

    async def reload_thread(
        self,
        *,
        user_id: int,
        thread_id: str,
    ) -> ConversationThread | None:
        """丢弃 identity map 缓存并读取最新会话摘要"""

        self._session.expire_all()
        return await self.get_thread(user_id=user_id, thread_id=thread_id)

    async def get_run(
        self,
        *,
        thread_pk: int,
        run_id: str,
    ) -> ConversationRunRegistration | None:
        """读取一个主 Run 注册"""

        return await self._session.scalar(
            select(ConversationRunRegistration).where(
                ConversationRunRegistration.conversation_thread_id == thread_pk,
                ConversationRunRegistration.run_id == run_id,
            )
        )

    async def get_run_for_update(
        self,
        *,
        thread_pk: int,
        run_id: str,
    ) -> ConversationRunRegistration | None:
        """锁定一个主 Run 注册用于幂等认领"""

        return await self._session.scalar(
            select(ConversationRunRegistration)
            .where(
                ConversationRunRegistration.conversation_thread_id == thread_pk,
                ConversationRunRegistration.run_id == run_id,
            )
            .with_for_update()
        )

    async def list_claims_for_update(
        self,
        *,
        thread_pk: int,
        interrupt_ids: frozenset[str],
    ) -> tuple[ConversationInterruptClaim, ...]:
        """锁定指定 interrupt 的现有业务认领"""

        if not interrupt_ids:
            return ()
        rows = await self._session.scalars(
            select(ConversationInterruptClaim)
            .where(
                ConversationInterruptClaim.conversation_thread_id == thread_pk,
                ConversationInterruptClaim.interrupt_id.in_(interrupt_ids),
            )
            .order_by(ConversationInterruptClaim.id)
            .with_for_update()
        )
        return tuple(rows)

    async def create_interrupt_claims(
        self,
        *,
        thread_pk: int,
        source_run_id: str,
        claimed_run_id: str,
        interrupt_ids: tuple[str, ...],
    ) -> tuple[ConversationInterruptClaim, ...]:
        """创建不含第三方 payload 的恢复认领记录"""

        now = datetime.now(UTC).replace(tzinfo=None)
        claims = tuple(
            ConversationInterruptClaim(
                conversation_thread_id=thread_pk,
                interrupt_id=interrupt_id,
                source_run_id=source_run_id,
                claimed_run_id=claimed_run_id,
                status="claimed",
                resolution_id=None,
                created_at=now,
                resolved_at=None,
                updated_at=now,
            )
            for interrupt_id in interrupt_ids
        )
        self._session.add_all(claims)
        await self._session.flush()
        return claims

    async def settle_claims(
        self,
        *,
        thread_pk: int,
        run_id: str,
        entries: tuple[ResumeEntry, ...],
        resolution_id: str,
    ) -> None:
        """按框架 checkpoint marker 幂等结算当前 Run 的全部认领"""

        claims = await self.list_claims_for_update(
            thread_pk=thread_pk,
            interrupt_ids=frozenset(entry.interrupt_id for entry in entries),
        )
        by_id = {claim.interrupt_id: claim for claim in claims}
        now = datetime.now(UTC).replace(tzinfo=None)
        for entry in entries:
            claim = by_id.get(entry.interrupt_id)
            if claim is None or claim.claimed_run_id != run_id:
                raise RuntimeError("恢复 checkpoint 缺少当前 Run 的完整认领")
            expected = "cancelled" if entry.status == "cancelled" else "resolved"
            if claim.resolution_id not in (None, resolution_id):
                raise RuntimeError("恢复认领已由不同 checkpoint marker 结算")
            if claim.status not in ("claimed", expected):
                raise RuntimeError("恢复认领状态与当前 checkpoint 冲突")
            claim.status = expected
            claim.resolution_id = resolution_id
            claim.resolved_at = now
            claim.updated_at = now

    async def release_claims(self, *, thread_pk: int, run_id: str) -> None:
        """删除尚未由 checkpoint marker 结算的当前 Run 认领"""

        await self._session.execute(
            delete(ConversationInterruptClaim).where(
                ConversationInterruptClaim.conversation_thread_id == thread_pk,
                ConversationInterruptClaim.claimed_run_id == run_id,
                ConversationInterruptClaim.status == "claimed",
            )
        )

    async def cancel_claims(
        self,
        *,
        thread_pk: int,
        run_id: str,
        resolution_id: str,
    ) -> None:
        """以 Trace abandonment 证据结算未继续执行的整批取消"""

        rows = await self._session.scalars(
            select(ConversationInterruptClaim)
            .where(
                ConversationInterruptClaim.conversation_thread_id == thread_pk,
                ConversationInterruptClaim.claimed_run_id == run_id,
            )
            .order_by(ConversationInterruptClaim.id)
            .with_for_update()
        )
        now = datetime.now(UTC).replace(tzinfo=None)
        for claim in rows:
            if claim.status == "cancelled" and claim.resolution_id == resolution_id:
                continue
            if claim.status != "claimed" or claim.resolution_id is not None:
                raise RuntimeError("恢复认领已由不同事实结算")
            claim.status = "cancelled"
            claim.resolution_id = resolution_id
            claim.resolved_at = now
            claim.updated_at = now

    async def list_threads(
        self,
        *,
        user_id: int,
        page_size: int,
        cursor: tuple[bool, datetime, int] | None,
        query: str | None = None,
    ) -> list[ConversationThread]:
        """按置顶与最近 Trace 活动执行稳定 keyset 分页"""

        statement = select(ConversationThread).where(
            ConversationThread.user_id == user_id,
            ConversationThread.deleted_at.is_(None),
        )
        if query is not None:
            escaped = (
                query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            statement = statement.where(
                ConversationThread.title.like(f"%{escaped}%", escape="\\")
            )
        if cursor is not None:
            pinned, updated_at, row_id = cursor
            statement = statement.where(
                or_(
                    ConversationThread.pinned < pinned,
                    and_(
                        ConversationThread.pinned == pinned,
                        ConversationThread.updated_at < updated_at,
                    ),
                    and_(
                        ConversationThread.pinned == pinned,
                        ConversationThread.updated_at == updated_at,
                        ConversationThread.id < row_id,
                    ),
                )
            )
        rows = await self._session.scalars(
            statement.order_by(
                ConversationThread.pinned.desc(),
                ConversationThread.updated_at.desc(),
                ConversationThread.id.desc(),
            ).limit(page_size + 1)
        )
        return list(rows)

    async def update_thread_meta(
        self,
        thread: ConversationThread,
        *,
        title: str | None,
        pinned: bool | None,
    ) -> None:
        """更新产品元信息且不伪造 Trace 最近活动时间"""

        # SQL 内递增序号，避免读到的 ORM 快照覆盖并发完成的标题
        values: dict[str, str | bool] = {}
        if title is not None:
            values.update(
                title=title, title_source="user", title_generation_status="skipped"
            )
        if pinned is not None:
            values["pinned"] = pinned
        statement = (
            update(ConversationThread)
            .where(ConversationThread.id == thread.id)
            .values(**values)
        )
        if title is not None:
            statement = statement.values(title_seq=ConversationThread.title_seq + 1)
        await self._update_title_row(statement)
        await self._session.refresh(thread)

    async def claim_title(self, thread_pk: int) -> bool:
        """持久认领一次自动总结；只有未尝试的临时标题可以调用模型"""
        return await self._update_title_row(
            update(ConversationThread)
            .where(
                ConversationThread.id == thread_pk,
                ConversationThread.title_source == "default",
                ConversationThread.title_generation_status == "idle",
                ConversationThread.deleted_at.is_(None),
                ConversationThread.status != "deleting",
            )
            .values(
                title_generation_status="running",
                title_seq=ConversationThread.title_seq + 1,
            )
        )

    async def finish_title(self, thread_pk: int, title: str | None) -> bool:
        """只结算仍由自动总结持有的标题；失败也不再自动尝试"""
        statement = (
            update(ConversationThread)
            .where(
                ConversationThread.id == thread_pk,
                ConversationThread.title_source == "default",
                ConversationThread.title_generation_status == "running",
                ConversationThread.deleted_at.is_(None),
                ConversationThread.status != "deleting",
            )
            .values(
                title_generation_status="succeeded" if title else "failed",
                title_seq=ConversationThread.title_seq + 1,
            )
        )
        if title is not None:
            statement = statement.values(title=title, title_source="generated")
        return await self._update_title_row(statement)

    async def _update_title_row(self, statement: Update) -> bool:
        result = await self._session.execute(
            statement.execution_options(synchronize_session=False)
        )
        if not isinstance(result, CursorResult):
            raise TypeError("会话更新未返回行数")
        return result.rowcount == 1

    async def update_trace_summary(
        self,
        *,
        thread_pk: int,
        run_id: str,
        status: str,
        message_count: int,
        tool_call_count: int,
        has_pending_interrupt: bool,
        pending_interaction_kind: str | None,
        terminal_outcome: str | None,
        error_code: str | None = None,
        updated_at: datetime,
        trace_generation: str,
        trace_as_of_seq: int,
        trace_observed_at: datetime,
    ) -> Literal["applied", "stale", "ambiguous", "generation_conflict"]:
        """按 Trace 前缀和存储观测时间拒绝迟到的旧摘要"""

        thread = await self.lock_thread(thread_pk)
        if thread is None:
            return "applied"
        registration = await self.get_run_for_update(thread_pk=thread_pk, run_id=run_id)
        if registration is not None:
            generation = registration.trace_generation
            if generation is not None and generation != trace_generation:
                return "generation_conflict"
            previous_seq = registration.trace_as_of_seq
            previous_time = registration.trace_observed_at
            if previous_seq is not None and previous_time is not None:
                current_order = (previous_seq, previous_time)
                incoming_order = (trace_as_of_seq, trace_observed_at)
                if incoming_order < current_order:
                    return "stale"
                if incoming_order == current_order:
                    same_result = (
                        registration.status
                        == _registration_status(status, terminal_outcome)
                        and registration.terminal_outcome == terminal_outcome
                        and registration.error_code == error_code
                    )
                    if thread.last_run_id in (None, run_id):
                        same_result = same_result and (
                            thread.status == status
                            and thread.message_count == message_count
                            and thread.tool_call_count == tool_call_count
                            and thread.has_pending_interrupt == has_pending_interrupt
                            and thread.pending_interaction_kind
                            == pending_interaction_kind
                        )
                    # 数据库时间精度内可能发生两次不同观测；不猜先后，调用方需重新读取
                    if not same_result:
                        return "ambiguous"
            registration.trace_generation = trace_generation
            registration.trace_as_of_seq = trace_as_of_seq
            registration.trace_observed_at = trace_observed_at
            registration.status = _registration_status(status, terminal_outcome)
            registration.terminal_outcome = terminal_outcome
            registration.error_code = error_code
            registration.finished_at = (
                updated_at if terminal_outcome is not None else None
            )
            registration.updated_at = max(registration.updated_at, updated_at)
        # 较早 Run 的延迟终态不能覆盖已经注册的新 head 摘要
        if thread.last_run_id not in (None, run_id):
            await self._session.flush()
            return "applied"
        thread.last_run_id = run_id
        thread.status = status
        thread.message_count = message_count
        thread.tool_call_count = tool_call_count
        thread.has_pending_interrupt = has_pending_interrupt
        thread.pending_interaction_kind = pending_interaction_kind
        thread.updated_at = max(thread.updated_at, updated_at)
        await self._session.flush()
        return "applied"

    async def lock_thread(self, thread_pk: int) -> ConversationThread | None:
        """锁定会话业务记录"""

        return await self._session.scalar(
            select(ConversationThread)
            .where(ConversationThread.id == thread_pk)
            .with_for_update()
        )

    async def has_running_run(self, thread_pk: int) -> bool:
        """返回是否仍有正在创建或执行的主 Run"""

        count = await self._session.scalar(
            select(func.count())
            .select_from(ConversationRunRegistration)
            .where(
                ConversationRunRegistration.conversation_thread_id == thread_pk,
                ConversationRunRegistration.status.in_(
                    ("preparing", "starting", "running")
                ),
            )
        )
        return bool(count)

    async def list_stale_unstarted_runs(
        self,
        *,
        older_than_seconds: int,
        thread_pk: int | None = None,
    ) -> tuple[ConversationRunRegistration, ...]:
        """读取超过启动宽限期且尚无 Trace 的注册"""

        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
            seconds=older_than_seconds
        )
        statement = select(ConversationRunRegistration).where(
            or_(
                and_(
                    ConversationRunRegistration.status == "preparing",
                    ConversationRunRegistration.created_at < cutoff,
                ),
                and_(
                    ConversationRunRegistration.status == "starting",
                    ConversationRunRegistration.updated_at < cutoff,
                ),
            )
        )
        if thread_pk is not None:
            statement = statement.where(
                ConversationRunRegistration.conversation_thread_id == thread_pk
            )
        rows = await self._session.scalars(
            statement.order_by(ConversationRunRegistration.id)
        )
        return tuple(rows)

    async def delete_unstarted_run(
        self,
        *,
        thread_pk: int,
        run_pk: int,
        run_id: str,
        delete_empty_thread: bool,
        expected_updated_at: datetime | None = None,
        allow_starting: bool = False,
    ) -> UnstartedRunCleanup:
        """删除未启动注册、未结算认领和可选空会话"""

        thread = await self.lock_thread(thread_pk)
        if thread is None:
            return UnstartedRunCleanup(run_deleted=False, thread_deleted=False)
        run = await self.get_run_for_update(thread_pk=thread_pk, run_id=run_id)
        deleted = False
        allowed_statuses = (
            {"preparing", "starting"} if allow_starting else {"preparing"}
        )
        if (
            run is not None
            and run.id == run_pk
            and run.conversation_thread_id == thread_pk
            and run.run_id == run_id
            and run.status in allowed_statuses
            and (expected_updated_at is None or run.updated_at == expected_updated_at)
        ):
            await self.release_claims(thread_pk=thread_pk, run_id=run_id)
            await self._session.delete(run)
            await self._session.flush()
            deleted = True
        thread_deleted = False
        if delete_empty_thread and deleted:
            remaining = await self._session.scalar(
                select(func.count())
                .select_from(ConversationRunRegistration)
                .where(ConversationRunRegistration.conversation_thread_id == thread_pk)
            )
            if not remaining:
                await self._session.delete(thread)
                thread_deleted = True
        if deleted and not thread_deleted and thread.last_run_id == run_id:
            previous = await self._session.scalar(
                select(ConversationRunRegistration)
                .where(ConversationRunRegistration.conversation_thread_id == thread_pk)
                .order_by(ConversationRunRegistration.id.desc())
                .limit(1)
            )
            if previous is None:
                thread.last_run_id = None
                thread.last_model = None
                thread.last_access_mode = "full"
                thread.status = "idle"
                thread.has_pending_interrupt = False
                thread.pending_interaction_kind = None
            else:
                thread.last_run_id = previous.run_id
                thread.last_model = previous.model_id
                thread.last_access_mode = previous.access_mode
                thread.status = _thread_status(previous.status)
        await self._session.flush()
        return UnstartedRunCleanup(
            run_deleted=deleted,
            thread_deleted=thread_deleted,
        )

    async def delete_thread_cascade(self, thread_pk: int) -> None:
        """删除 Studio 自有认领、Run 注册和会话业务记录"""

        await self._session.execute(
            delete(ConversationInterruptClaim).where(
                ConversationInterruptClaim.conversation_thread_id == thread_pk
            )
        )
        await self._session.execute(
            delete(ConversationRunRegistration).where(
                ConversationRunRegistration.conversation_thread_id == thread_pk
            )
        )
        await self._session.execute(
            delete(ConversationThread).where(ConversationThread.id == thread_pk)
        )

    async def commit(self) -> None:
        """提交调用方业务事务"""

        await self._session.commit()

    async def rollback(self) -> None:
        """回滚调用方业务事务"""

        await self._session.rollback()


def _registration_status(status: str, outcome: str | None) -> str:
    if status == "waiting_approval":
        return "waiting"
    if status == "error":
        return "failed"
    if outcome is not None:
        return outcome
    return status


def _thread_status(registration_status: str) -> str:
    if registration_status in {"preparing", "starting", "running"}:
        return "running"
    if registration_status in {"waiting", "interrupted"}:
        return "waiting_approval"
    if registration_status == "failed":
        return "error"
    return "idle"


__all__ = ["ConversationRepository", "UnstartedRunCleanup"]
