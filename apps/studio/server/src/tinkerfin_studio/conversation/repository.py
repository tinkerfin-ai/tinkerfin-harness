"""会话归属、Run 注册、恢复认领与列表摘要数据访问"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import JsonValue
from sqlalchemy import and_, delete, false, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Update

from tinkerfin import AgUiResumeReceipt
from tinkerfin_studio.agent.access import AccessMode
from tinkerfin_studio.conversation.models import (
    ConversationInterruptClaim,
    ConversationRunRegistration,
    ConversationThread,
)
from tinkerfin_studio.services.schemas import ServiceBindings


@dataclass(frozen=True, slots=True)
class UnstartedRunCleanup:
    """未启动登记清理结果；resume_released 表示已有可信的未保存证明"""

    run_deleted: bool
    thread_deleted: bool
    resume_released: bool = False


@dataclass(frozen=True, slots=True)
class TraceSummaryWrite:
    """摘要应用结果及需要刷新列表的会话，逐字输出不触发列表刷新"""

    status: Literal["applied", "stale", "ambiguous", "generation_conflict"]
    changed_thread: ConversationThread | None = None


class ConversationRepository:
    """在调用方事务内维护 Studio 自有会话数据"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_thread(
        self,
        *,
        user_id: int,
        project_id: str,
        thread_id: str,
        title: str,
        model_id: str | None,
    ) -> ConversationThread:
        """创建不含正文的用户会话记录"""

        now = datetime.now(UTC).replace(tzinfo=None)
        entity = ConversationThread(
            user_id=user_id,
            project_id=project_id,
            archived=False,
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
        service_bindings: ServiceBindings = ServiceBindings.empty(),
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
            service_bindings=service_bindings.model_dump(mode="json"),
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
            .execution_options(populate_existing=True)
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

    async def list_interaction_claims(
        self,
        *,
        thread_pk: int,
        interrupt_ids: frozenset[str],
        submission_run_id: str,
    ) -> tuple[ConversationInterruptClaim, ...]:
        """读取待处理交互和当前提交的认领，不加载其他轮次的审批历史"""

        rows = await self._session.scalars(
            select(ConversationInterruptClaim)
            .where(
                ConversationInterruptClaim.conversation_thread_id == thread_pk,
                or_(
                    ConversationInterruptClaim.interrupt_id.in_(interrupt_ids),
                    ConversationInterruptClaim.claimed_run_id == submission_run_id,
                ),
            )
            .order_by(ConversationInterruptClaim.id)
            .execution_options(populate_existing=True)
        )
        return tuple(rows)

    async def get_runs(
        self, *, thread_pk: int, run_ids: frozenset[str]
    ) -> tuple[ConversationRunRegistration, ...]:
        """批量读取同一会话的提交快照，供已结算交互恢复原决定"""

        if not run_ids:
            return ()
        rows = await self._session.scalars(
            select(ConversationRunRegistration).where(
                ConversationRunRegistration.conversation_thread_id == thread_pk,
                ConversationRunRegistration.run_id.in_(run_ids),
            )
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

        registration = await self.get_run(thread_pk=thread_pk, run_id=claimed_run_id)
        if registration is not None and registration.resume_not_saved:
            raise RuntimeError("已确认未保存的提交不能重新认领")
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
        receipt: AgUiResumeReceipt,
    ) -> None:
        """按恢复回执中的公开审批结果幂等结算当前运行的认领"""

        await self.lock_thread(thread_pk)
        registration = await self.get_run_for_update(
            thread_pk=thread_pk, run_id=receipt.identity.run_id
        )
        if registration is not None and registration.resume_not_saved:
            raise RuntimeError("恢复回执与已确认未保存的提交冲突")
        claims = await self.list_claims_for_update(
            thread_pk=thread_pk,
            interrupt_ids=frozenset(
                response.interrupt_id for response in receipt.responses
            ),
        )
        by_id = {claim.interrupt_id: claim for claim in claims}
        now = datetime.now(UTC).replace(tzinfo=None)
        for response in receipt.responses:
            claim = by_id.get(response.interrupt_id)
            if claim is None or claim.claimed_run_id != receipt.identity.run_id:
                raise RuntimeError("恢复回执缺少当前运行的完整认领")
            expected = response.status
            if claim.resolution_id not in (None, receipt.receipt_id):
                raise RuntimeError("恢复认领已由不同回执结算")
            if claim.status not in ("claimed", expected):
                raise RuntimeError("恢复认领状态与当前回执冲突")
            claim.status = expected
            claim.resolution_id = receipt.receipt_id
            claim.resolved_at = now
            claim.updated_at = now

    async def release_claims(
        self, *, thread_pk: int, run_id: str
    ) -> ConversationRunRegistration | None:
        """在已证明未保存时保留提交结果并释放认领

        仅由框架未保存回调或本次未受理投递的所有者调用。调用方负责提交；
        原登记上的确认标记与认领释放在同一事务内完成。后台清理只能复用
        已存在的标记，不能从缺失观测推断未保存。

        Args:
            thread_pk: 已核实归属的会话主键
            run_id: 已确认未保存的恢复提交 ID

        Returns:
            已标记的原运行登记；尚无确认标记且没有认领，或登记已删除时返回 None

        Raises:
            RuntimeError: 已有保存或取消结果，或未保存证明与当前认领冲突
        """

        if await self.lock_thread(thread_pk) is None:
            return None
        registration = await self.get_run_for_update(thread_pk=thread_pk, run_id=run_id)
        if registration is None:
            return None
        claims = tuple(
            await self._session.scalars(
                select(ConversationInterruptClaim)
                .where(
                    ConversationInterruptClaim.conversation_thread_id == thread_pk,
                    ConversationInterruptClaim.claimed_run_id == run_id,
                )
                .order_by(ConversationInterruptClaim.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        if registration.resume_not_saved:
            if claims:
                raise RuntimeError("已确认未保存的提交仍持有认领")
            return registration
        if not claims:
            return None
        if any(claim.status != "claimed" or claim.resolution_id for claim in claims):
            raise RuntimeError("已保存或取消的决定不能结算为未保存")
        resume = registration.input_json.get("resume")
        if not isinstance(resume, list) or not resume:
            raise RuntimeError("恢复认领缺少原始提交")
        registration.resume_not_saved = True
        await self._session.execute(
            delete(ConversationInterruptClaim).where(
                ConversationInterruptClaim.conversation_thread_id == thread_pk,
                ConversationInterruptClaim.claimed_run_id == run_id,
                ConversationInterruptClaim.status == "claimed",
            )
        )
        return registration

    async def cancel_claims(
        self,
        *,
        thread_pk: int,
        run_id: str,
        resolution_id: str,
    ) -> None:
        """以 Trace abandonment 证据结算未继续执行的整批取消"""

        await self.lock_thread(thread_pk)
        registration = await self.get_run_for_update(thread_pk=thread_pk, run_id=run_id)
        if registration is not None and registration.resume_not_saved:
            raise RuntimeError("取消凭据与已确认未保存的提交冲突")
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
        project_id: str | None = None,
        archived: bool = False,
    ) -> list[ConversationThread]:
        """按置顶与最近 Trace 活动分页，仅公开已有历史读取入口的会话"""

        statement = select(ConversationThread).where(
            ConversationThread.user_id == user_id,
            ConversationThread.deleted_at.is_(None),
            ConversationThread.archived.is_(archived),
            ConversationThread.last_run_id.is_not(None),
        )
        if project_id is not None:
            statement = statement.where(ConversationThread.project_id == project_id)
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
                    ConversationThread.pinned.is_(False) if pinned else false(),
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

    async def organize_thread(
        self,
        *,
        user_id: int,
        thread_id: str,
        project_id: str | None,
        archived: bool | None,
    ) -> ConversationThread:
        """在注册运行使用的会话锁内改变项目或归档状态"""
        from tinkerfin_studio.api.errors import (
            BusinessException,
            ConversationErrorCode,
            ProjectErrorCode,
        )
        from tinkerfin_studio.projects.repository import ProjectRepository

        thread = await self.get_thread(user_id=user_id, thread_id=thread_id)
        if thread is None:
            raise BusinessException(ConversationErrorCode.NOT_FOUND)
        thread = await self.lock_thread(thread.id)
        if (
            thread is None
            or thread.deleted_at is not None
            or thread.status == "deleting"
        ):
            raise BusinessException(ConversationErrorCode.NOT_FOUND)
        changing = (project_id is not None and project_id != thread.project_id) or (
            archived is not None and archived != thread.archived
        )
        if changing and (
            thread.has_pending_interrupt
            or thread.status in {"running", "waiting_approval"}
            or await self.has_running_run(thread.id)
        ):
            raise BusinessException(ProjectErrorCode.ACTIVE_CONVERSATION)
        if project_id is not None:
            await ProjectRepository(self._session, user_id).require(project_id)
            from tinkerfin_studio.attachments.entity import AttachmentFile

            await self._session.execute(
                update(AttachmentFile)
                .where(
                    AttachmentFile.user_id == user_id,
                    AttachmentFile.thread_id == thread_id,
                )
                .values(project_id=project_id)
            )
            thread.project_id = project_id
        if archived is not None:
            thread.archived = archived
        await self._session.flush()
        return thread

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
        if not values:
            return
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
    ) -> TraceSummaryWrite:
        """按 Trace 前缀和存储观测时间拒绝迟到的旧摘要"""

        thread = await self.lock_thread(thread_pk)
        if thread is None or thread.status == "deleting":
            return TraceSummaryWrite("applied")
        registration = await self.get_run_for_update(thread_pk=thread_pk, run_id=run_id)
        # 准备阶段 Trace 不证明业务已受理，不能消耗所有者的清理权或发布 head
        admission_status = (
            registration.status
            if registration is not None
            and registration.status in ("preparing", "rejected")
            else None
        )
        registration_status = admission_status or _registration_status(
            status, terminal_outcome
        )
        if registration is not None:
            generation = registration.trace_generation
            if generation is not None and generation != trace_generation:
                return TraceSummaryWrite("generation_conflict")
            previous_seq = registration.trace_as_of_seq
            previous_time = registration.trace_observed_at
            if previous_seq is not None and previous_time is not None:
                current_order = (previous_seq, previous_time)
                incoming_order = (trace_as_of_seq, trace_observed_at)
                if incoming_order < current_order:
                    return TraceSummaryWrite("stale")
                if incoming_order == current_order:
                    same_result = (
                        (
                            registration.status == "starting"
                            or registration.status == registration_status
                        )
                        and registration.terminal_outcome == terminal_outcome
                        and registration.error_code == error_code
                    )
                    # starting 是确认后的首次投影；同前缀准备诊断尚未成为列表摘要
                    if (
                        admission_status is None
                        and registration.status != "starting"
                        and thread.last_run_id in (None, run_id)
                    ):
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
                        return TraceSummaryWrite("ambiguous")
            registration.trace_generation = trace_generation
            registration.trace_as_of_seq = trace_as_of_seq
            registration.trace_observed_at = trace_observed_at
            registration.status = registration_status
            registration.terminal_outcome = terminal_outcome
            registration.error_code = error_code
            registration.finished_at = (
                updated_at if terminal_outcome is not None else None
            )
            if admission_status is None:
                registration.updated_at = max(registration.updated_at, updated_at)
        # 未受理请求继续保留真实 Trace 诊断，但不能成为会话 head
        # 较早 Run 的延迟终态也不能覆盖已经注册的新 head 摘要
        if admission_status is not None or thread.last_run_id not in (None, run_id):
            await self._session.flush()
            return TraceSummaryWrite("applied")
        # 更新时间随逐字输出推进；只有列表内容变化才发送列表失效提示
        visible_changed = (
            thread.last_run_id != run_id
            or thread.status != status
            or thread.message_count != message_count
            or thread.tool_call_count != tool_call_count
            or thread.has_pending_interrupt != has_pending_interrupt
            or thread.pending_interaction_kind != pending_interaction_kind
        )
        thread.last_run_id = run_id
        thread.status = status
        thread.message_count = message_count
        thread.tool_call_count = tool_call_count
        thread.has_pending_interrupt = has_pending_interrupt
        thread.pending_interaction_kind = pending_interaction_kind
        thread.updated_at = max(thread.updated_at, updated_at)
        await self._session.flush()
        return TraceSummaryWrite("applied", thread if visible_changed else None)

    async def lock_thread(self, thread_pk: int) -> ConversationThread | None:
        """锁定会话业务记录"""

        return await self._session.scalar(
            select(ConversationThread)
            .where(ConversationThread.id == thread_pk)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def has_running_run(self, thread_pk: int) -> bool:
        """调用方持有会话锁，以当前读取确认是否仍有正在准备或执行的运行"""

        run_id = await self._session.scalar(
            select(ConversationRunRegistration.id)
            .where(
                ConversationRunRegistration.conversation_thread_id == thread_pk,
                ConversationRunRegistration.status.in_(
                    ("preparing", "starting", "running")
                ),
            )
            .limit(1)
            .with_for_update()
        )
        return run_id is not None

    async def list_pending_runs(
        self, *, thread_pk: int | None = None
    ) -> tuple[tuple[ConversationThread, ConversationRunRegistration], ...]:
        """读取需要与 Trace 校准的运行登记，不占用会话写锁"""

        statement = (
            select(ConversationThread, ConversationRunRegistration)
            .join(
                ConversationRunRegistration,
                ConversationRunRegistration.conversation_thread_id
                == ConversationThread.id,
            )
            .where(
                ConversationThread.deleted_at.is_(None),
                ConversationThread.status != "deleting",
                ConversationRunRegistration.status.in_(
                    ("preparing", "starting", "running")
                ),
            )
            .order_by(ConversationRunRegistration.id)
        )
        if thread_pk is not None:
            statement = statement.where(ConversationThread.id == thread_pk)
        rows = await self._session.execute(statement)
        return tuple((thread, run) for thread, run in rows)

    async def delete_unstarted_run(
        self,
        *,
        thread_pk: int,
        run_pk: int,
        run_id: str,
        preparation_id: str,
        delete_empty_thread: bool,
        expected_updated_at: datetime | None = None,
        expected_status: Literal["preparing", "starting"] = "preparing",
        resume_not_saved: bool = False,
    ) -> UnstartedRunCleanup:
        """清理未启动登记；已确认未保存的恢复提交保留为未受理记录"""

        thread = await self.lock_thread(thread_pk)
        if thread is None:
            return UnstartedRunCleanup(run_deleted=False, thread_deleted=False)
        run = await self.get_run_for_update(thread_pk=thread_pk, run_id=run_id)
        deleted = False
        resume_released = False
        # 激活可发生在同一时间精度内，清理必须同时匹配检查时的业务状态
        if (
            run is not None
            and run.id == run_pk
            and run.conversation_thread_id == thread_pk
            and run.run_id == run_id
            and run.preparation_id == preparation_id
            and run.status == expected_status
            and (expected_updated_at is None or run.updated_at == expected_updated_at)
        ):
            resume = run.input_json.get("resume")
            if isinstance(resume, list) and resume:
                # 失活或缺少 Trace 不证明未保存；只有本次所有者或已有标记能结算
                if not resume_not_saved and not (
                    run.resume_not_saved and run.status == "preparing"
                ):
                    return UnstartedRunCleanup(run_deleted=False, thread_deleted=False)
                resume_released = (
                    await self.release_claims(thread_pk=thread_pk, run_id=run_id)
                ) is not None
                if not resume_released:
                    return UnstartedRunCleanup(run_deleted=False, thread_deleted=False)
                # rejected 是业务未受理，不覆盖准备阶段可能已写入的 Trace 终态
                run.status = "rejected"
                run.updated_at = datetime.now(UTC).replace(tzinfo=None)
            else:
                await self._session.delete(run)
                deleted = True
            await self._session.flush()
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
        if (
            (deleted or resume_released)
            and not thread_deleted
            and thread.last_run_id == run_id
        ):
            previous = await self._session.scalar(
                select(ConversationRunRegistration)
                .where(
                    ConversationRunRegistration.conversation_thread_id == thread_pk,
                    ConversationRunRegistration.status.not_in(
                        ("preparing", "rejected")
                    ),
                )
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
            resume_released=resume_released,
        )

    async def delete_thread_cascade(self, thread_pk: int) -> None:
        """删除会话的认领、保留提交结果的 Run 注册和业务记录"""
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
