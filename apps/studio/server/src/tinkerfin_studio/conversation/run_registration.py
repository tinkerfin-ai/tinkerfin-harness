"""会话 thread 解析、恢复认领与主 Run 事务注册"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin import AgUiResumeRequest
from tinkerfin_notifications import Notifications
from tinkerfin_studio.api.errors import (
    BusinessException,
    ConversationErrorCode,
    ModelErrorCode,
    ServiceErrorCode,
)
from tinkerfin_studio.attachments.service import AttachmentService
from tinkerfin_studio.changes import notify_change
from tinkerfin_studio.conversation.models import (
    ConversationRunRegistration,
    ConversationThread,
)
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.conversation.run_preparation import (
    ChatIntent,
    CompactIntent,
    PreparedRunRequest,
    RegisteredRun,
    ResumeChatIntent,
    StartChatIntent,
    conversation_identity,
)
from tinkerfin_studio.models.repository import AgentModelRepository
from tinkerfin_studio.models.schemas import AgentModelConfig
from tinkerfin_studio.models.service import model_settings, resolved_model
from tinkerfin_studio.projects.repository import ProjectRepository
from tinkerfin_studio.services.repository import ServiceConfigRepository
from tinkerfin_studio.services.schemas import ServiceBindings
from tinkerfin_studio.services.service import (
    ResolvedService,
    ServiceConfigService,
    bindings_for,
)
from tinkerfin_studio.skills.repository import SkillRepository
from tinkerfin_studio.skills.schemas import SkillSnapshotPayload


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
    skills: SkillSnapshotPayload
    search_service: ResolvedService | None
    image_service: ResolvedService | None
    thread_created: bool = False


class ConversationRunPreparer:
    """拥有 chat 启动前短事务，不读取 Agent 内部状态或审批 payload"""

    def __init__(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        attachments: AttachmentService,
        notifications: Notifications,
    ) -> None:
        self._attachments = attachments
        self._notifications = notifications
        self._session = session
        self._user_id = user_id
        self._repository = ConversationRepository(session)

    async def resolve_thread(
        self,
        *,
        thread_id: str,
        run_id: str,
        project_id: str,
        intent: ChatIntent,
    ) -> ResolvedThread:
        """解析已有会话，或按 Run 幂等创建新会话"""

        await ProjectRepository(self._session, self._user_id).require(project_id)
        if thread_id:
            thread = await self._repository.get_thread(
                user_id=self._user_id,
                thread_id=thread_id,
            )
            if thread is None or thread.project_id != project_id:
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
            if thread.project_id != project_id:
                raise BusinessException(ConversationErrorCode.RUN_CONFLICT)
            return ResolvedThread(thread=thread, created=False)
        created = False
        try:
            # 新会话与首条运行登记一起提交，校验失败不能留下无法读取的空会话
            thread = await self._repository.create_thread(
                user_id=self._user_id,
                project_id=project_id,
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
            if locked.project_id != prepared.project_id:
                raise BusinessException(ConversationErrorCode.RUN_CONFLICT)
            if locked.archived:
                raise BusinessException(ConversationErrorCode.RUN_CONFLICT)
            thread = locked
            # 同一登记可能已被另一请求建立；锁内重新读取，避免使用锁前的快照
            existing = await self._repository.get_run_for_update(
                thread_pk=thread.id, run_id=prepared.identity.run_id
            )
            if existing is not None and existing.resume_not_saved:
                raise BusinessException(ConversationErrorCode.RESUME_NOT_SAVED)
            self._require_same_registration(existing, prepared=prepared, model=model)
            source_run_id = self._continuation_source_run_id(
                intent,
                prepared=prepared,
                thread=thread,
            )
            source_run: ConversationRunRegistration | None = None
            if source_run_id is not None:
                source_run = await self._repository.get_run_for_update(
                    thread_pk=thread.id,
                    run_id=source_run_id,
                )
                if source_run is None or source_run.status == "rejected":
                    raise BusinessException(ConversationErrorCode.RUN_NOT_FOUND)
                self._require_source_model(source_run, model=model)
                if source_run.access_mode != prepared.access_mode:
                    raise BusinessException(ConversationErrorCode.RUN_IDENTITY_CONFLICT)
            services = ServiceConfigService(
                ServiceConfigRepository(self._session, user_id=self._user_id)
            )
            if isinstance(intent, CompactIntent):
                search_service = None
                image_service = None
            elif isinstance(intent, ResumeChatIntent):
                if source_run is None:
                    raise BusinessException(ConversationErrorCode.RUN_NOT_FOUND)
                source_bindings = ServiceBindings.model_validate(
                    source_run.service_bindings
                )
                search_service = (
                    None
                    if source_bindings.web_search is None
                    else await services.require_bound(
                        "web_search",
                        id=source_bindings.web_search.id,
                        fingerprint=source_bindings.web_search.fingerprint,
                    )
                )
                image_service = (
                    None
                    if source_bindings.image_generation is None
                    else await services.require_bound(
                        "image_generation",
                        id=source_bindings.image_generation.id,
                        fingerprint=source_bindings.image_generation.fingerprint,
                    )
                )
            else:
                search_service = await services.resolve("web_search", for_update=True)
                image_service = await services.resolve(
                    "image_generation", for_update=True
                )
            bindings = bindings_for(search_service, image_service)
            if (
                existing is not None
                and ServiceBindings.model_validate(existing.service_bindings)
                != bindings
            ):
                raise BusinessException(ServiceErrorCode.CONFIGURATION_CHANGED)
            if isinstance(intent, ResumeChatIntent):
                if source_run_id is None:
                    raise BusinessException(ConversationErrorCode.RESUME_REQUIRED)
                await self._claim_resume(
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
                    bindings=bindings,
                )
            self._require_same_registration(existing, prepared=prepared, model=model)
            if ServiceBindings.model_validate(existing.service_bindings) != bindings:
                raise BusinessException(ServiceErrorCode.CONFIGURATION_CHANGED)
            if not created and existing.status in {"preparing", "starting"}:
                # 共享登记后的失败请求不再拥有独占清理权；过期恢复同样比较此标识
                existing.preparation_id = uuid4().hex
                existing.updated_at = datetime.now(UTC).replace(tzinfo=None)
            skills = SkillRepository(self._session, self._user_id)
            if created:
                # 审批恢复保留原选择记录；执行文件由工作区读取当前内容
                source_id = (
                    source_run_id if isinstance(intent, ResumeChatIntent) else None
                )
                skill_snapshot = await skills.capture(
                    prepared.identity,
                    project_id=prepared.project_id,
                    selected_ids=intent.skill_ids
                    if isinstance(intent, StartChatIntent)
                    else (),
                    source=None
                    if source_id is None
                    else conversation_identity(
                        thread.thread_id,
                        source_id,
                        user_id=self._user_id,
                    ),
                )
            else:
                skill_snapshot = await skills.snapshot(prepared.identity)
            await self._repository.commit()
            if isinstance(intent, StartChatIntent) and intent.attachments:
                await notify_change(
                    self._notifications,
                    user_id=self._user_id,
                    topic="studio.attachments.changed",
                    key=thread.thread_id,
                    details={"thread_id": thread.thread_id},
                )
            return PreparedExecution(
                thread=thread,
                skills=skill_snapshot,
                search_service=search_service,
                image_service=image_service,
                registered=RegisteredRun(
                    run_id=existing.id,
                    created=created,
                    preparation_id=existing.preparation_id,
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
        thread_id: str,
        identity_run_id: str,
        registered: RegisteredRun,
        thread_created: bool,
    ) -> None:
        """清理本次未受理登记，保留恢复提交的未保存结果

        请求取消不会中断已接受的清理，调用方等到数据库操作结束后才收到取消。
        清理期间独占借用的请求会话；已有登记只结束当前事务，不删除记录。

        Args:
            thread_pk: 已校验归属的会话主键
            thread_id: 当前会话的公开标识
            identity_run_id: 当前请求的运行 ID
            registered: 本次登记结果，决定是否拥有清理权限
            thread_created: 是否允许一并删除本次新建的空会话

        Raises:
            BaseException: 数据库清理失败或请求取消，保留同时发生的异常原因
        """

        async def cleanup() -> BaseException | None:
            try:
                if not registered.created:
                    await self._repository.rollback()
                else:
                    result = await self._repository.delete_unstarted_run(
                        thread_pk=thread_pk,
                        run_pk=registered.run_id,
                        run_id=identity_run_id,
                        preparation_id=registered.preparation_id,
                        delete_empty_thread=thread_created,
                        resume_not_saved=True,
                    )
                    await self._repository.commit()
                    if result.resume_released:
                        await notify_change(
                            self._notifications,
                            user_id=self._user_id,
                            topic="studio.conversation.interactions.changed",
                            key=thread_id,
                            details={"submissionRunId": identity_run_id},
                        )
                    if result.run_deleted or result.resume_released:
                        await notify_change(
                            self._notifications,
                            user_id=self._user_id,
                            topic="studio.conversation.changed",
                            key=thread_id,
                        )
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
        thread_id: str,
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
        await notify_change(
            self._notifications,
            user_id=self._user_id,
            topic="studio.conversation.changed",
            key=thread_id,
        )

    async def _claim_resume(
        self,
        intent: ResumeChatIntent,
        *,
        prepared: PreparedRunRequest,
        thread: ConversationThread,
        source_run_id: str,
    ) -> None:
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
        bindings: ServiceBindings,
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
                    service_bindings=bindings,
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
            if ServiceBindings.model_validate(existing.service_bindings) != bindings:
                raise BusinessException(ServiceErrorCode.CONFIGURATION_CHANGED)
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
