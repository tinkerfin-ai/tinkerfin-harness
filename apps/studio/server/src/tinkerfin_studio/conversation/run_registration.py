"""会话 thread 解析、恢复认领与主 Run 事务注册"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin import AgUiResumeRequest
from tinkerfin_studio.api.errors import (
    BusinessException,
    ConversationErrorCode,
    ModelErrorCode,
)
from tinkerfin_studio.attachments.service import AttachmentService
from tinkerfin_studio.conversation.models import (
    ConversationRunRegistration,
    ConversationThread,
)
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.conversation.run_preparation import (
    ChatIntent,
    PreparedRunRequest,
    RegisteredRun,
    ResumeChatIntent,
    StartChatIntent,
)
from tinkerfin_studio.models.repository import AgentModelRepository
from tinkerfin_studio.models.schemas import AgentModelConfig
from tinkerfin_studio.models.service import model_settings, resolved_model


@dataclass(frozen=True, slots=True)
class ResolvedThread:
    """返回解析后的 thread 及其是否由当前请求创建"""

    thread: ConversationThread
    created: bool


@dataclass(frozen=True, slots=True)
class PreparedExecution:
    """外部流启动前已提交的 thread、Run 注册与恢复请求"""

    thread: ConversationThread
    registered: RegisteredRun
    resume: AgUiResumeRequest | None
    thread_created: bool = False


class ConversationRunPreparer:
    """拥有 chat 启动前短事务，不读取 Agent 内部状态或审批 payload"""

    def __init__(
        self, session: AsyncSession, *, user_id: int, attachments: AttachmentService
    ) -> None:
        self._attachments = attachments
        self._session = session
        self._user_id = user_id
        self._repository = ConversationRepository(session)

    async def resolve_thread(
        self,
        *,
        thread_id: str,
        run_id: str,
        intent: ChatIntent,
    ) -> ResolvedThread:
        """解析已有会话，或按 Run 幂等创建新会话"""

        if thread_id:
            thread = await self._repository.get_thread(
                user_id=self._user_id,
                thread_id=thread_id,
            )
            if thread is None:
                raise BusinessException(ConversationErrorCode.NOT_FOUND)
            return ResolvedThread(thread=thread, created=False)
        if not isinstance(intent, StartChatIntent):
            raise BusinessException(ConversationErrorCode.USER_MESSAGE_REQUIRED)
        generated_thread_id = "thread-" + str(
            uuid5(
                NAMESPACE_URL,
                f"tinkerfin-studio:{self._user_id}:run:{run_id}",
            )
        )
        thread = await self._repository.get_thread(
            user_id=self._user_id,
            thread_id=generated_thread_id,
        )
        if thread is not None:
            return ResolvedThread(thread=thread, created=False)
        created = False
        try:
            async with self._session.begin_nested():
                thread = await self._repository.create_thread(
                    user_id=self._user_id,
                    thread_id=generated_thread_id,
                    title=intent.title,
                    model_id=None,
                )
                created = True
        except IntegrityError:
            await self._repository.rollback()
            thread = await self._repository.get_thread(
                user_id=self._user_id,
                thread_id=generated_thread_id,
            )
            if thread is None:
                raise
        await self._repository.commit()
        return ResolvedThread(thread=thread, created=created)

    async def register(
        self,
        *,
        intent: ChatIntent,
        prepared: PreparedRunRequest,
        model: AgentModelConfig,
        thread: ConversationThread,
        thread_created: bool = False,
    ) -> PreparedExecution:
        """原子认领客户端 interrupt ID 并固定 Run 的模型"""

        existing = await self._repository.get_run(
            thread_pk=thread.id,
            run_id=prepared.identity.run_id,
        )
        self._require_same_registration(existing, prepared=prepared, model=model)
        claimed_ids: frozenset[str] = frozenset()
        try:
            models = AgentModelRepository(self._session, user_id=self._user_id)
            await models.lock_owner()
            current_model = await models.get_for_update(model.model_id)
            current_connection = (
                await models.connection(current_model.connection_id, for_update=True)
                if current_model is not None
                else None
            )
            if (
                current_model is None
                or not current_model.enabled
                or current_connection is None
            ):
                raise BusinessException(ModelErrorCode.CONFIGURATION_CHANGED)
            current = resolved_model(model_settings(current_model), current_connection)
            if current != model:
                raise BusinessException(ModelErrorCode.CONFIGURATION_CHANGED)
            locked = await self._repository.lock_thread(thread.id)
            if locked is None or locked.status == "deleting":
                raise BusinessException(ConversationErrorCode.NOT_FOUND)
            thread = locked
            source_run_id = self._continuation_source_run_id(
                intent,
                prepared=prepared,
                thread=thread,
            )
            if source_run_id is not None:
                source_run = await self._repository.get_run_for_update(
                    thread_pk=thread.id,
                    run_id=source_run_id,
                )
                if source_run is None:
                    raise BusinessException(ConversationErrorCode.RUN_NOT_FOUND)
                self._require_source_model(source_run, model=model)
                if source_run.access_mode != prepared.access_mode:
                    raise BusinessException(ConversationErrorCode.RUN_IDENTITY_CONFLICT)
            if isinstance(intent, ResumeChatIntent):
                if source_run_id is None:
                    raise BusinessException(ConversationErrorCode.RESUME_REQUIRED)
                claimed_ids = await self._claim_resume(
                    intent,
                    prepared=prepared,
                    thread=thread,
                    source_run_id=source_run_id,
                )
            elif existing is None and thread.has_pending_interrupt:
                raise BusinessException(ConversationErrorCode.PENDING_INTERRUPT)

            if isinstance(intent, StartChatIntent):
                ids = [attachment.id for attachment in intent.attachments]
                await self._attachments.bind(
                    self._session,
                    ids,
                    user_id=self._user_id,
                    thread_id=thread.thread_id,
                    message_id=prepared.message_ids[0],
                )
            created = False
            if existing is None:
                existing, created = await self._create_run(
                    prepared=prepared,
                    model=model,
                    thread=thread,
                )
            self._require_same_registration(existing, prepared=prepared, model=model)
            await self._repository.commit()
            return PreparedExecution(
                thread=thread,
                registered=RegisteredRun(
                    run_id=existing.id,
                    created=created,
                    claimed_interrupt_ids=claimed_ids,
                ),
                resume=(
                    AgUiResumeRequest(entries=intent.entries)
                    if isinstance(intent, ResumeChatIntent)
                    else None
                ),
                thread_created=thread_created,
            )
        except BaseException:
            await self._repository.rollback()
            raise

    async def cleanup_unstarted(
        self,
        *,
        thread_pk: int,
        identity_run_id: str,
        registered: RegisteredRun,
        thread_created: bool,
    ) -> None:
        """删除本次未启动的运行登记，并等待清理提交完成

        请求取消不会中断已接受的清理，调用方等到数据库操作结束后才收到取消。
        清理期间独占借用的请求会话；已有登记只结束当前事务，不删除记录。

        Args:
            thread_pk: 已校验归属的会话主键
            identity_run_id: 当前请求的运行 ID
            registered: 本次登记结果，决定是否拥有删除权限
            thread_created: 是否允许一并删除本次新建的空会话

        Raises:
            BaseException: 数据库清理失败或请求取消，保留同时发生的异常原因
        """

        async def cleanup() -> BaseException | None:
            try:
                if not registered.created:
                    await self._repository.rollback()
                else:
                    await self._repository.delete_unstarted_run(
                        thread_pk=thread_pk,
                        run_pk=registered.run_id,
                        run_id=identity_run_id,
                        delete_empty_thread=thread_created,
                    )
                    await self._repository.commit()
            except BaseException as error:  # noqa: BLE001 - 交回请求处理方，避免后台任务丢失控制异常
                return error
            return None

        task = asyncio.create_task(cleanup(), name="conversation-registration-cleanup")
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                cancellation = cancellation or error
        failure = task.result()
        if cancellation is not None:
            if failure is not None:
                primary: BaseException = cancellation
                secondary: BaseException = failure
                if not isinstance(failure, (Exception, asyncio.CancelledError)):
                    primary, secondary = failure, cancellation
                # 让 Python 保留两条原始异常链；不覆盖供应商已有的 cause
                try:
                    raise secondary
                except BaseException:  # noqa: BLE001 - 按控制异常、取消、普通失败的顺序交付
                    raise primary
            raise cancellation
        if failure is not None:
            raise failure

    async def activate_started(
        self,
        *,
        thread_pk: int,
        identity_run_id: str,
        registered: RegisteredRun,
    ) -> None:
        """在 Messaging owner 已建立后以 CAS 激活业务 Run"""

        try:
            activated = await self._repository.activate_run_registration(
                thread_pk=thread_pk,
                run_pk=registered.run_id,
                run_id=identity_run_id,
            )
            if not activated:
                raise BusinessException(ConversationErrorCode.RUN_CONFLICT)
            await self._repository.commit()
        except BaseException:
            await self._repository.rollback()
            raise

    async def _claim_resume(
        self,
        intent: ResumeChatIntent,
        *,
        prepared: PreparedRunRequest,
        thread: ConversationThread,
        source_run_id: str,
    ) -> frozenset[str]:
        """认领客户端 ID，完整性与 Tool correlation 由框架 Checkpointer 校验"""

        claimed_ids = frozenset(entry.interrupt_id for entry in intent.entries)
        existing = await self._repository.list_claims_for_update(
            thread_pk=thread.id,
            interrupt_ids=claimed_ids,
        )
        if any(
            claim.claimed_run_id != prepared.identity.run_id
            or claim.source_run_id != source_run_id
            for claim in existing
        ):
            raise BusinessException(ConversationErrorCode.RESUME_ALREADY_CLAIMED)
        existing_ids = {claim.interrupt_id for claim in existing}
        missing = tuple(
            entry.interrupt_id
            for entry in intent.entries
            if entry.interrupt_id not in existing_ids
        )
        if missing:
            try:
                async with self._session.begin_nested():
                    await self._repository.create_interrupt_claims(
                        thread_pk=thread.id,
                        source_run_id=source_run_id,
                        claimed_run_id=prepared.identity.run_id,
                        interrupt_ids=missing,
                    )
            except IntegrityError as error:
                raise BusinessException(
                    ConversationErrorCode.RESUME_ALREADY_CLAIMED
                ) from error
        return claimed_ids

    @staticmethod
    def _continuation_source_run_id(
        intent: ChatIntent,
        *,
        prepared: PreparedRunRequest,
        thread: ConversationThread,
    ) -> str | None:
        """解析 resume 或 branch 必须继承模型合同的来源 Run"""

        if isinstance(intent, ResumeChatIntent):
            return prepared.parent_run_id or thread.last_run_id
        return prepared.parent_run_id

    @staticmethod
    def _require_source_model(
        source: ConversationRunRegistration,
        *,
        model: AgentModelConfig,
    ) -> None:
        """拒绝客户端用不同模型继续已有 checkpoint"""

        if source.model_id != model.model_id:
            raise BusinessException(ConversationErrorCode.RUN_IDENTITY_CONFLICT)

    async def _create_run(
        self,
        *,
        prepared: PreparedRunRequest,
        model: AgentModelConfig,
        thread: ConversationThread,
    ) -> tuple[ConversationRunRegistration, bool]:
        """创建或读取同一幂等 Run 注册"""

        thread_id = thread.thread_id
        try:
            async with self._session.begin_nested():
                created = await self._repository.create_run_registration(
                    thread_id=thread.id,
                    run_id=prepared.identity.run_id,
                    parent_run_id=prepared.parent_run_id,
                    model_id=model.model_id,
                    input_json=prepared.input_json,
                    access_mode=prepared.access_mode,
                )
                return created, True
        except IntegrityError:
            await self._repository.rollback()
            refreshed = await self._repository.get_thread(
                user_id=self._user_id,
                thread_id=thread_id,
            )
            if refreshed is None:
                raise
            existing = await self._repository.get_run(
                thread_pk=refreshed.id,
                run_id=prepared.identity.run_id,
            )
            if existing is None:
                raise
            self._require_same_registration(existing, prepared=prepared, model=model)
            return existing, False

    @staticmethod
    def _require_same_registration(
        run: ConversationRunRegistration | None,
        *,
        prepared: PreparedRunRequest,
        model: AgentModelConfig,
    ) -> None:
        """拒绝同 Run 改写输入或模型"""

        if run is None:
            return
        if run.input_json != prepared.input_json or run.model_id != model.model_id:
            raise BusinessException(ConversationErrorCode.RUN_IDENTITY_CONFLICT)


__all__ = ["ConversationRunPreparer", "PreparedExecution", "ResolvedThread"]
