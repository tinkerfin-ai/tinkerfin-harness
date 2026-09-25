"""会话 chat 与取消服务"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass

from ag_ui.core import (
    BaseEvent,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
)
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin import AgUiResumeReceipt, AgUiRunStream, RunIdentity, SseBody
from tinkerfin_messaging import (
    parse_sse_event_id,
)
from tinkerfin_messaging.errors import (
    MessagingError,
    MessagingErrorCode,
    RunProducerFailed,
)
from tinkerfin_studio.agent.runtime import build_conversation_runtime
from tinkerfin_studio.api.errors import (
    AttachmentErrorCode,
    BusinessException,
    ConversationErrorCode,
    SystemException,
)
from tinkerfin_studio.auth.types import UserContext
from tinkerfin_studio.conversation.error_logging import log_conversation_error
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.conversation.request import ChatRequest, CompactRequest
from tinkerfin_studio.conversation.run_preparation import (
    ChatIntent,
    CompactIntent,
    PreparedRunRequest,
    StartChatIntent,
    classify_intent,
    conversation_identity,
    decorate_main_event,
    prepare_run_request,
)
from tinkerfin_studio.conversation.run_registration import (
    ConversationRunPreparer,
    PreparedExecution,
)
from tinkerfin_studio.conversation.schemas import (
    CancelRunResponse,
)
from tinkerfin_studio.models.repository import AgentModelRepository
from tinkerfin_studio.models.schemas import AgentModelConfig
from tinkerfin_studio.models.service import AgentModelService
from tinkerfin_studio.resources import ApplicationResources
from tinkerfin_tracing import TraceThreadNotFound, TracingError

_MESSAGING_ERRORS: dict[
    MessagingErrorCode,
    tuple[ConversationErrorCode, bool],
] = {
    MessagingErrorCode.ERROR: (ConversationErrorCode.MESSAGING_FAILURE, False),
    MessagingErrorCode.NOT_STARTED: (
        ConversationErrorCode.MESSAGING_FAILURE,
        False,
    ),
    MessagingErrorCode.SETTLEMENT_TIMEOUT: (
        ConversationErrorCode.MESSAGING_UNAVAILABLE,
        False,
    ),
    MessagingErrorCode.CLOSED: (ConversationErrorCode.MESSAGING_FAILURE, False),
    MessagingErrorCode.INVALID_CURSOR: (
        ConversationErrorCode.INVALID_LAST_EVENT_ID,
        True,
    ),
    MessagingErrorCode.CODEC_MISMATCH: (
        ConversationErrorCode.MESSAGING_FAILURE,
        False,
    ),
    MessagingErrorCode.SOURCE_PROFILE_MISMATCH: (
        ConversationErrorCode.MESSAGING_FAILURE,
        False,
    ),
    MessagingErrorCode.PUBLICATION_REJECTED: (
        ConversationErrorCode.MESSAGING_FAILURE,
        False,
    ),
    MessagingErrorCode.MESSAGE_ID_CONFLICT: (
        ConversationErrorCode.RUN_IDENTITY_CONFLICT,
        True,
    ),
    MessagingErrorCode.QUOTA_EXCEEDED: (
        ConversationErrorCode.MESSAGING_QUOTA_EXCEEDED,
        True,
    ),
    MessagingErrorCode.RUN_ALREADY_ACTIVE: (
        ConversationErrorCode.RUN_CONFLICT,
        True,
    ),
    MessagingErrorCode.RUN_NOT_FOUND: (
        ConversationErrorCode.RUN_NOT_FOUND,
        True,
    ),
    MessagingErrorCode.RUN_PRODUCER_FAILED: (
        ConversationErrorCode.MESSAGING_FAILURE,
        False,
    ),
    MessagingErrorCode.CANCELLATION_UNSUPPORTED: (
        ConversationErrorCode.RUN_CANCEL_UNSUPPORTED,
        True,
    ),
    MessagingErrorCode.RECOVERY_UNSUPPORTED: (
        ConversationErrorCode.MESSAGING_FAILURE,
        False,
    ),
    MessagingErrorCode.SSE_RENDERING_UNSUPPORTED: (
        ConversationErrorCode.MESSAGING_FAILURE,
        False,
    ),
    MessagingErrorCode.BACKEND_OWNERSHIP_LOST: (
        ConversationErrorCode.MESSAGING_FAILURE,
        False,
    ),
    MessagingErrorCode.STREAM_EXPIRED: (
        ConversationErrorCode.MESSAGING_STREAM_EXPIRED,
        True,
    ),
    MessagingErrorCode.STREAM_DELETED: (
        ConversationErrorCode.RUN_NOT_FOUND,
        True,
    ),
    MessagingErrorCode.STREAM_DELETE_CONFLICT: (
        ConversationErrorCode.DELETE_CONFLICT,
        True,
    ),
    MessagingErrorCode.BACKEND_UNAVAILABLE: (
        ConversationErrorCode.MESSAGING_UNAVAILABLE,
        False,
    ),
    MessagingErrorCode.BACKEND_TIMEOUT: (
        ConversationErrorCode.MESSAGING_UNAVAILABLE,
        False,
    ),
    MessagingErrorCode.BACKEND_PROTOCOL_ERROR: (
        ConversationErrorCode.MESSAGING_FAILURE,
        False,
    ),
    MessagingErrorCode.UNEXPECTED_BACKEND_FAILURE: (
        ConversationErrorCode.MESSAGING_FAILURE,
        False,
    ),
}


def parse_last_event_id(value: str | None) -> int | None:
    """解析规范十进制 SSE 重连游标"""

    try:
        return parse_sse_event_id(value)
    except (TypeError, ValueError) as error:
        raise BusinessException(ConversationErrorCode.INVALID_LAST_EVENT_ID) from error


@dataclass(frozen=True, slots=True)
class PreparedChat:
    """完成 Messaging 预握手后的 HTTP SSE 内容"""

    body: AsyncGenerator[bytes, None] | SseBody[bytes]
    thread_id: str


class ConversationChatService:
    """准备请求级 Agent 事件源并启动或附着 durable AG-UI run"""

    def __init__(
        self,
        session: AsyncSession,
        *,
        user: UserContext,
        resources: ApplicationResources,
    ) -> None:
        self._session = session
        self._user = user
        self._resources = resources
        self._repository = ConversationRepository(session)

    async def start(
        self,
        request: ChatRequest | CompactRequest,
        *,
        last_event_id: str | None,
        thread_id: str = "",
    ) -> PreparedChat:
        """完成业务校验、Agent 事件源准备和 Messaging 预握手"""

        after = parse_last_event_id(last_event_id)
        models = AgentModelService(
            AgentModelRepository(self._session, user_id=self._user.user_id)
        )
        intent: ChatIntent
        if isinstance(request, CompactRequest):
            model = await models.resolve(request.model)
            image_model = None
            intent = CompactIntent(thread_id=thread_id)
        else:
            model = await models.resolve(request.forwarded_props.model)
            request = await self._resolve_attachments(request)
            image_model = await models.resolve_image_model()
            intent = classify_intent(request)
            thread_id = request.thread_id
        run_preparer, prepared, execution = await self._prepare_execution(
            request,
            intent=intent,
            model=model,
            thread_id=thread_id,
        )
        try:
            events = self._create_events(
                intent=intent,
                execution=execution,
                prepared=prepared,
                model=model,
                image_model=image_model,
            )
        except BaseException:
            # 流尚未交给消息服务，本次业务登记仍由请求负责清理
            await run_preparer.cleanup_unstarted(
                thread_pk=execution.thread.id,
                identity_run_id=prepared.identity.run_id,
                registered=execution.registered,
                thread_created=execution.thread_created,
            )
            raise
        body = await self._start_delivery(
            events,
            after=after,
            prepared=prepared,
            execution=execution,
            run_preparer=run_preparer,
            title_text=request.user_input.text
            if isinstance(request, ChatRequest) and isinstance(intent, StartChatIntent)
            else "",
            model=model,
            image_model=image_model,
        )
        return PreparedChat(body=body, thread_id=execution.thread.thread_id)

    async def _resolve_attachments(self, request: ChatRequest) -> ChatRequest:
        """以有权读取的仓储信息替换客户端附件描述，核验文件总量"""
        if not request.messages:
            return request
        submission = request.user_input
        attachments = []
        total = 0
        for attachment_id in submission.attachment_ids:
            attachment = await self._resources.attachments.get(
                attachment_id,
                user_id=self._user.user_id,
                thread_id=request.thread_id or None,
            )
            total += attachment.size_bytes
            attachments.append(attachment)
        if total > 25 * 1024 * 1024:
            raise BusinessException(AttachmentErrorCode.TOO_LARGE)
        payload = request.model_dump()
        payload["messages"] = [
            submission.with_attachments(attachments).model_dump(mode="json")
        ]
        return ChatRequest.model_validate(payload)

    async def _prepare_execution(
        self,
        request: ChatRequest | CompactRequest,
        *,
        intent: ChatIntent,
        model: AgentModelConfig,
        thread_id: str,
    ) -> tuple[ConversationRunPreparer, PreparedRunRequest, PreparedExecution]:
        """完成 thread 解析、权威快照和短事务 run 注册"""

        run_preparer = ConversationRunPreparer(
            self._session,
            user_id=self._user.user_id,
            attachments=self._resources.attachments,
        )
        resolved_thread = await run_preparer.resolve_thread(
            thread_id=thread_id,
            run_id=request.run_id,
            intent=intent,
        )
        thread = resolved_thread.thread
        # 释放 thread 查询产生的只读事务，恢复检查不得占用请求连接或数据库锁
        await self._repository.commit()
        await self._resources.conversation_trace.recover_preparing(
            thread_pk=thread.id,
        )
        refreshed = await self._repository.reload_thread(
            user_id=self._user.user_id,
            thread_id=thread.thread_id,
        )
        if refreshed is None:
            if thread_id:
                raise BusinessException(ConversationErrorCode.NOT_FOUND)
            resolved_thread = await run_preparer.resolve_thread(
                thread_id=thread_id,
                run_id=request.run_id,
                intent=intent,
            )
            thread = resolved_thread.thread
        else:
            thread = refreshed
        if thread.last_run_id and thread.last_run_id != request.run_id:
            # 新请求登记前先刷新上一 head，不能用 Messaging 传输状态推断 Agent 结果
            # Trace reconcile 会借用同一共享 Engine；先结束 reload 产生的只读事务
            await self._repository.commit()
            previous_identity = conversation_identity(
                thread.thread_id,
                thread.last_run_id,
                user_id=self._user.user_id,
            )
            try:
                await self._resources.conversation_trace.reconcile(
                    thread_pk=thread.id,
                    identity=previous_identity,
                )
            except TraceThreadNotFound as error:
                raise SystemException(
                    ConversationErrorCode.TRACE_UNAVAILABLE
                ) from error
            thread = await self._repository.reload_thread(
                user_id=self._user.user_id,
                thread_id=thread.thread_id,
            )
            if thread is None:
                raise BusinessException(ConversationErrorCode.NOT_FOUND)
            if thread.status == "running":
                raise BusinessException(ConversationErrorCode.RUN_CONFLICT)
        prepared = prepare_run_request(
            request,
            user_id=self._user.user_id,
            thread_id=thread.thread_id,
            access_mode=thread.last_access_mode,
        )
        try:
            execution = await run_preparer.register(
                intent=intent,
                prepared=prepared,
                model=model,
                thread=thread,
                thread_created=resolved_thread.created,
            )
        except MessagingError as error:
            raise self._messaging_error(error) from error
        return run_preparer, prepared, execution

    def _create_events(
        self,
        *,
        intent: ChatIntent,
        execution: PreparedExecution,
        prepared: PreparedRunRequest,
        model: AgentModelConfig,
        image_model: AgentModelConfig | None,
    ) -> AgUiRunStream:
        """创建会话事件流，在执行开始时准备运行资源"""

        runtime = build_conversation_runtime(
            resources=self._resources,
            user_id=self._user.user_id,
            thread_id=execution.thread.thread_id,
            model_config=model,
            image_model=image_model,
            access_mode=prepared.access_mode,
        )
        if isinstance(intent, CompactIntent):
            # 框架整理已保存的会话上下文，复用会话投递与取消，不提交草稿或业务工具调用
            return runtime.agui.open_compaction(
                thread_id=prepared.identity.thread_id,
                run_id=prepared.identity.run_id,
            )

        resume_request = None
        if isinstance(intent, StartChatIntent):
            messages = prepared.messages
        else:
            if execution.resume is None:
                raise RuntimeError("恢复请求缺少 AgUiResumeRequest")
            messages = None
            resume_request = execution.resume

        async def record_saved(receipt: AgUiResumeReceipt) -> None:
            await self._resources.conversation_trace.settle_resume(
                thread_pk=execution.thread.id,
                receipt=receipt,
            )

        async def release_resume_claims() -> None:
            async with self._resources.database.session() as session:
                repository = ConversationRepository(session)
                await repository.release_claims(
                    thread_pk=execution.thread.id,
                    run_id=prepared.identity.run_id,
                )
                await repository.commit()

        if resume_request is None:
            assert messages is not None
            events = runtime.open_agui_run(
                thread_id=prepared.identity.thread_id,
                run_id=prepared.identity.run_id,
                messages=messages,
                parent_run_id=prepared.parent_run_id,
                mode=prepared.mode,
                config=prepared.graph_config,
            )
        else:
            events = runtime.open_agui_run(
                thread_id=prepared.identity.thread_id,
                run_id=prepared.identity.run_id,
                resume=resume_request,
                parent_run_id=prepared.parent_run_id,
                mode=prepared.mode,
                config=prepared.graph_config,
                on_resume_saved=record_saved,
                on_resume_not_saved=release_resume_claims,
            )
        return events

    async def _start_delivery(
        self,
        events: AgUiRunStream,
        *,
        after: int | None,
        prepared: PreparedRunRequest,
        execution: PreparedExecution,
        run_preparer: ConversationRunPreparer,
        title_text: str,
        model: AgentModelConfig,
        image_model: AgentModelConfig | None,
    ) -> AsyncGenerator[bytes, None] | SseBody[bytes]:
        """接入会话事件持久化与重连回放，并返回 SSE 内容"""

        async def decorate_event(event: BaseEvent) -> BaseEvent:
            return decorate_main_event(
                event,
                prepared=prepared,
                title=execution.thread.title,
                title_source=execution.thread.title_source,
                title_seq=execution.thread.title_seq,
                title_generation_status=execution.thread.title_generation_status,
            )

        async def run_started(_event: RunStartedEvent) -> None:
            if (
                title_text.strip()
                and execution.thread.title_source == "default"
                and execution.thread.title_generation_status == "idle"
            ):
                await self._resources.conversation_titles.start(
                    thread_pk=execution.thread.id,
                    text=title_text,
                    model=model,
                )

        async def run_finished(event: RunFinishedEvent | RunErrorEvent) -> None:
            if isinstance(event, RunErrorEvent) and event.code != "cancelled":
                await log_conversation_error(
                    identity=prepared.identity,
                    model=model,
                    image_model=image_model,
                    code=event.code,
                    error=events.error,
                )

        async def activate_ready_source() -> None:
            """在框架 Run 可查询后发布业务 head"""

            await run_preparer.activate_started(
                thread_pk=execution.thread.id,
                identity_run_id=prepared.identity.run_id,
                registered=execution.registered,
            )

        async def cleanup_not_started() -> None:
            """清理没有 producer 且没有 attachment 的业务注册"""

            await run_preparer.cleanup_unstarted(
                thread_pk=execution.thread.id,
                identity_run_id=prepared.identity.run_id,
                registered=execution.registered,
                thread_created=execution.thread_created,
            )

        async def subscription_ready() -> None:
            """每次成功订阅均确保会话摘要持续更新"""

            self._resources.conversation_trace.ensure(
                thread_pk=execution.thread.id,
                identity=prepared.identity,
            )

        try:
            body = await self._resources.conversation_channel.open_sse(
                events,
                after=after,
                on_source_ready=activate_ready_source,
                on_subscribed=subscription_ready,
                transform_event=decorate_event,
                on_run_started=run_started,
                on_run_finished=run_finished,
                on_delivery_not_started=cleanup_not_started,
            )
        except MessagingError as error:
            raise self._messaging_error(error) from error
        return body

    async def cancel(self, *, thread_id: str, run_id: str) -> CancelRunResponse:
        """验证用户归属后请求并等待 durable run 取消"""

        thread = await self._repository.get_thread(
            user_id=self._user.user_id,
            thread_id=thread_id,
        )
        if thread is None:
            raise BusinessException(ConversationErrorCode.NOT_FOUND)
        thread_pk = thread.id
        if await self._repository.get_run(thread_pk=thread_pk, run_id=run_id) is None:
            raise BusinessException(ConversationErrorCode.RUN_NOT_FOUND)
        # 提交隐式只读事务可释放连接，并保留活跃流仍会读取的 thread 事实
        await self._repository.commit()
        try:
            identity = conversation_identity(
                thread_id, run_id, user_id=self._user.user_id
            )
            cancelled = await self._resources.conversation_channel.cancel(
                identity=identity,
            )
        except RunProducerFailed:
            # run 已经失败时“停止”是幂等确认，用户应回到可重试状态而不是看到 500
            await self._reconcile_trace(thread_pk=thread_pk, identity=identity)
            return CancelRunResponse(cancelled=False)
        except MessagingError as error:
            raise self._messaging_error(error, operation="cancel") from error
        await self._reconcile_trace(thread_pk=thread_pk, identity=identity)
        return CancelRunResponse(cancelled=cancelled)

    async def _reconcile_trace(
        self,
        *,
        thread_pk: int,
        identity: RunIdentity,
    ) -> None:
        """把取消后的 Runtime 终态同步为列表摘要"""

        try:
            await self._resources.conversation_trace.reconcile(
                thread_pk=thread_pk,
                identity=identity,
            )
        except TracingError as error:
            raise SystemException(ConversationErrorCode.TRACE_UNAVAILABLE) from error

    @staticmethod
    def _messaging_error(
        error: MessagingError,
        *,
        operation: str = "chat",
    ) -> BusinessException | SystemException:
        error_code, business = _MESSAGING_ERRORS[error.code]
        if (
            operation == "cancel"
            and error.code is MessagingErrorCode.RUN_PRODUCER_FAILED
        ):
            error_code = ConversationErrorCode.RUN_CANCEL_FAILED
        exception_type = BusinessException if business else SystemException
        return exception_type(error_code)


__all__ = [
    "ConversationChatService",
    "PreparedChat",
    "parse_last_event_id",
]
