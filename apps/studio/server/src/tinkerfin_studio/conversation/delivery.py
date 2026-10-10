"""会话受理登记、审批结算与已提交事件的业务处理"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ag_ui.core import RunErrorEvent, RunFinishedEvent, RunStartedEvent

from tinkerfin import AgUiResumeReceipt, RunIdentity
from tinkerfin_gateway import CommittedRunEvent, RunAcceptance
from tinkerfin_studio.changes import notify_change
from tinkerfin_studio.conversation.error_logging import log_conversation_error
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.conversation.run_preparation import RegisteredRun
from tinkerfin_studio.conversation.run_registration import ConversationRunPreparer
from tinkerfin_studio.models.schemas import AgentModelConfig
from tinkerfin_studio.services.service import ResolvedService

if TYPE_CHECKING:
    from tinkerfin_studio.resources import ApplicationResources


class ConversationAdmission:
    """结算一次会话提交，后台操作只借用短事务

    新执行准备完成后激活会话；附着只恢复摘要跟随，不提前激活尚在准备的执行。
    释放只处理本次拥有的登记，已经开始的运行由权威轨迹继续结算。
    """

    def __init__(
        self,
        resources: ApplicationResources,
        *,
        user_id: int,
        thread_pk: int,
        identity: RunIdentity,
        registered: RegisteredRun,
        thread_created: bool,
    ) -> None:
        self._resources = resources
        self._user_id = user_id
        self._thread_pk = thread_pk
        self._identity = identity
        self._registered = registered
        self._thread_created = thread_created

    async def confirm(self, acceptance: RunAcceptance) -> None:
        """确认新执行或已有投递，并保持列表摘要更新"""
        if acceptance.identity != self._identity:
            raise ValueError("会话受理身份不匹配")
        if acceptance.kind == "new":
            async with self._resources.database.session() as session:
                await ConversationRunPreparer(
                    session,
                    user_id=self._user_id,
                    attachments=self._resources.attachments,
                    notifications=self._resources.notifications,
                ).activate_started(
                    thread_pk=self._thread_pk,
                    thread_id=self._identity.thread_id,
                    identity_run_id=self._identity.run_id,
                    registered=self._registered,
                )
        else:
            self._resources.conversation_trace.ensure(
                thread_pk=self._thread_pk, identity=self._identity
            )

    async def release(self) -> None:
        """清理未受理的本次登记，不保留已结束的请求会话"""
        async with self._resources.database.session() as session:
            await ConversationRunPreparer(
                session,
                user_id=self._user_id,
                attachments=self._resources.attachments,
                notifications=self._resources.notifications,
            ).cleanup_unstarted(
                thread_pk=self._thread_pk,
                thread_id=self._identity.thread_id,
                identity_run_id=self._identity.run_id,
                registered=self._registered,
                thread_created=self._thread_created,
            )


class ConversationResumeSettlement:
    """按框架确认的审批保存结果结算认领，不把受理当作已保存"""

    def __init__(
        self, resources: ApplicationResources, *, thread_pk: int, run_id: str
    ) -> None:
        self._resources = resources
        self._thread_pk = thread_pk
        self._run_id = run_id

    async def saved(self, receipt: AgUiResumeReceipt) -> None:
        """按持久化回执更新审批记录和会话摘要"""
        await self._resources.conversation_trace.settle_resume(
            thread_pk=self._thread_pk, receipt=receipt
        )

    async def not_saved(self) -> None:
        """仅在明确未保存审批时释放本运行的认领"""
        async with self._resources.database.session() as session:
            repository = ConversationRepository(session)
            released = await repository.release_claims(
                thread_pk=self._thread_pk, run_id=self._run_id
            )
            thread = await repository.get_thread_by_pk(self._thread_pk)
            await repository.commit()
        if released is not None and thread is not None:
            await notify_change(
                self._resources.notifications,
                user_id=thread.user_id,
                topic="studio.conversation.interactions.changed",
                key=thread.thread_id,
                details={"submissionRunId": self._run_id},
            )


class ConversationRunObserver:
    """启动标题总结、记录失败并在运行结束后刷新记忆，重播不重复执行"""

    def __init__(
        self,
        resources: ApplicationResources,
        *,
        thread_pk: int,
        user_id: int,
        project_id: str,
        title_text: str,
        model: AgentModelConfig,
        search_service: ResolvedService | None,
        image_service: ResolvedService | None,
    ) -> None:
        self._resources = resources
        self._thread_pk = thread_pk
        self._user_id = user_id
        self._project_id = project_id
        self._title_text = title_text
        self._model = model
        self._search_service = search_service
        self._image_service = image_service

    async def __call__(self, committed: CommittedRunEvent) -> None:
        """接收主运行已提交的开始或终止事件"""
        event = committed.event
        if isinstance(event, RunStartedEvent):
            # 列表跟随与标题都是已受理运行的附带工作，失败不能撤销主运行
            try:
                self._resources.conversation_trace.ensure(
                    thread_pk=self._thread_pk, identity=committed.identity
                )
            finally:
                if self._title_text.strip():
                    await self._resources.conversation_titles.start(
                        thread_pk=self._thread_pk,
                        text=self._title_text,
                        model=self._model,
                    )
        elif isinstance(event, (RunFinishedEvent, RunErrorEvent)):
            try:
                if isinstance(event, RunErrorEvent) and event.code != "cancelled":
                    await log_conversation_error(
                        identity=committed.identity,
                        model=self._model,
                        search_service=self._search_service,
                        image_service=self._image_service,
                        code=event.code,
                        error=committed.diagnostic_error,
                    )
            finally:
                # Agent 与页面共用记忆集合；主运行结束后提示页面重读其持久写入
                await notify_change(
                    self._resources.notifications,
                    user_id=self._user_id,
                    topic="studio.memories.changed",
                    key=self._project_id,
                )
