"""FastAPI 生命周期拥有的外部资源"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable
from contextlib import (
    AbstractAsyncContextManager,
    AsyncExitStack,
    asynccontextmanager,
)
from dataclasses import dataclass
from types import TracebackType
from typing import NoReturn, TypeVar, cast

import httpx
from fastapi import FastAPI
from opensandbox.config import ConnectionConfig
from redis.asyncio import Redis

from tinkerfin import TinkerFin
from tinkerfin_automation import Automation, SqlAlchemyAutomationStore
from tinkerfin_gateway import Gateway
from tinkerfin_messaging import AgUiChannel, MessagingLimits, MessagingRetentionPolicy
from tinkerfin_messaging.messaging import Messaging
from tinkerfin_messaging.redis import RedisBackend
from tinkerfin_notifications import Notifications
from tinkerfin_notifications.redis import RedisBackend as NotificationRedisBackend
from tinkerfin_sandbox.lifecycle.client import OpenSandboxClient
from tinkerfin_sandbox.lifecycle.manager import OpenSandboxManager
from tinkerfin_sandbox.lifecycle.sqlalchemy import SQLAlchemyOpenSandboxState
from tinkerfin_sandbox.models import OpenSandboxConfig
from tinkerfin_studio.agent.persistence import AgentPersistence
from tinkerfin_studio.agent.subagents import SubagentSettings, load_subagents
from tinkerfin_studio.attachments.minio import MinioAttachmentStorage
from tinkerfin_studio.attachments.service import AttachmentService
from tinkerfin_studio.automation.target import (
    StudioAutomationTarget,
    fail_interactive_execution,
)
from tinkerfin_studio.config.logging import setup_logging
from tinkerfin_studio.config.settings import Settings, get_settings
from tinkerfin_studio.conversation.coordinator import (
    ConversationTraceCoordinator,
)
from tinkerfin_studio.conversation.failures import ConversationFailureProjection
from tinkerfin_studio.conversation.history_queries import HistoryQueryAdmission
from tinkerfin_studio.conversation.titles import ConversationTitles
from tinkerfin_studio.conversation.todo_groups import TodoGroupProjection
from tinkerfin_studio.health import ReadinessService
from tinkerfin_studio.infrastructure._failures import _cleanup_failure_priority
from tinkerfin_studio.infrastructure.database import Database
from tinkerfin_studio.infrastructure.redis_client import create_redis_client
from tinkerfin_studio.infrastructure.redis_keys import (
    MESSAGING_KEY_PREFIX,
    NOTIFICATIONS_CHANNEL,
)
from tinkerfin_studio.infrastructure.sandbox_events import SandboxEventLogger
from tinkerfin_studio.models.transport import ModelTransport
from tinkerfin_studio.skills.content import SkillContentStore
from tinkerfin_studio.skills.downloads import GitHubSkillImporter
from tinkerfin_studio.skills.github_source import GitHubSkillSource
from tinkerfin_studio.skills.library import SkillLibrary
from tinkerfin_studio.skills.packages import SkillArchiveReader
from tinkerfin_studio.skills.sources import ClawHubSkillSource, SkillSources
from tinkerfin_tracing import (
    CapturePolicy,
    SqlAlchemyTraceStore,
    Tracer,
)

logger = logging.getLogger(__name__)
_STUDIO_MESSAGING_LIMITS = MessagingLimits(
    max_message_payload_bytes=4 * 1024 * 1024,
)
_STUDIO_MESSAGING_RETENTION = MessagingRetentionPolicy.expire_after(24 * 60 * 60)
_ResourceT = TypeVar("_ResourceT")


class _LifespanOutcome:
    """保存生命周期主体异常，供每个资源退出边界读取同一主因"""

    def __init__(self) -> None:
        self.error: BaseException | None = None
        self.traceback: TracebackType | None = None

    def capture(self, error: BaseException) -> None:
        self.error = error
        self.traceback = error.__traceback__


async def _enter_lifespan_context(
    stack: AsyncExitStack,
    outcome: _LifespanOutcome,
    resource: AbstractAsyncContextManager[_ResourceT],
) -> _ResourceT:
    """进入资源并保证退出时收到生命周期主体的原始异常"""

    entered = await resource.__aenter__()

    async def close(
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> bool:
        del _exc_type, _exc_value, _traceback
        error = outcome.error
        return bool(
            await resource.__aexit__(
                None if error is None else type(error),
                error,
                outcome.traceback,
            )
        )

    stack.push_async_exit(close)
    return entered


async def _enter_conversation_resources(
    stack: AsyncExitStack,
    outcome: _LifespanOutcome,
    *,
    messaging: Messaging,
    database: Database,
    notifications: Notifications,
    tracer: Tracer,
) -> tuple[AgUiChannel, Gateway, ConversationTraceCoordinator]:
    """保持摘要服务可用，直到执行关闭及最后的业务状态校准完成"""

    conversations = await _enter_lifespan_context(stack, outcome, AsyncExitStack())
    await _enter_lifespan_context(conversations, outcome, messaging)
    channel = messaging.agui_channel(name="studio-conversation-agui")
    gateway = Gateway(
        messaging=messaging, notifications=notifications, name=channel.name
    )
    coordinator = ConversationTraceCoordinator(
        database=database,
        notifications=notifications,
        tracer=tracer,
        conversation_channel=channel,
    )
    # 转移已登记的关闭职责；初始化失败也保留相同的资源所有权与异常结算
    execution = conversations.pop_all()
    conversations.push_async_callback(
        _close_conversation_resources, execution, coordinator
    )
    await coordinator.recover()
    return channel, gateway, coordinator


async def _close_conversation_resources(
    execution: AsyncExitStack, coordinator: ConversationTraceCoordinator
) -> None:
    """依次结束执行、校准摘要并关闭跟随，任何失败都不能跳过后续清理"""

    failures: list[BaseException] = []
    for close in (execution.aclose, coordinator.settle, coordinator.aclose):
        try:
            await close()
        except BaseException as error:  # noqa: BLE001 - 全部清理后交付失败
            failures.append(error)
    cancellation = next(
        (error for error in failures if isinstance(error, asyncio.CancelledError)), None
    )
    failures = [error for error in failures if error is not cancellation]
    cleanup_error = (
        failures[0]
        if len(failures) == 1
        else BaseExceptionGroup("会话关闭失败", failures)
        if failures
        else None
    )
    outcome = _LifespanOutcome()
    if cancellation is not None:
        outcome.capture(cancellation)
    if cancellation is not None or cleanup_error is not None:
        _raise_lifespan_outcome(outcome, cleanup_error)


def _raise_lifespan_outcome(
    outcome: _LifespanOutcome,
    cleanup_error: BaseException | None,
) -> NoReturn:
    """按主异常、进程控制与清理失败的固定优先级重新抛出"""

    primary = outcome.error
    if primary is not None:
        if cleanup_error is None:
            raise primary.with_traceback(outcome.traceback)
        if primary.__cause__ is not None and primary.__cause__ is not cleanup_error:
            cleanup_error = BaseExceptionGroup(
                "原异常原因与资源清理失败", [primary.__cause__, cleanup_error]
            )
        if (
            _cleanup_failure_priority(primary) < 2
            and _cleanup_failure_priority(cleanup_error) == 2
        ):
            if isinstance(primary, asyncio.CancelledError):
                raise BaseExceptionGroup(
                    "关闭取消与进程控制同时发生", [cleanup_error, primary]
                )
            raise cleanup_error.with_traceback(cleanup_error.__traceback__) from primary
        raise primary.with_traceback(outcome.traceback) from cleanup_error
    assert cleanup_error is not None
    raise cleanup_error.with_traceback(cleanup_error.__traceback__)


async def _settle_lifespan_stack(
    stack: AsyncExitStack,
    outcome: _LifespanOutcome,
) -> None:
    """拥有全部资源退出任务，调用者取消须等待清理结束后传播"""

    primary = outcome.error

    async def close() -> BaseException | None:
        try:
            await stack.__aexit__(
                None if primary is None else type(primary), primary, outcome.traceback
            )
        except BaseException as error:  # noqa: BLE001 - 将进程控制也交回生命周期调用者
            return error
        return None

    closing = asyncio.create_task(close(), name="studio-resources-close")
    cancellation: asyncio.CancelledError | None = None
    while not closing.done():
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError as error:
            cancellation = cancellation or error
    cleanup_error = closing.result()
    if cancellation is not None:
        failures = [error for error in (primary, cleanup_error) if error is not None]
        interruption = _LifespanOutcome()
        interruption.capture(cancellation)
        _raise_lifespan_outcome(
            interruption,
            failures[0]
            if len(failures) == 1
            else BaseExceptionGroup("生命周期与清理失败", failures)
            if failures
            else None,
        )
    if primary is not None or cleanup_error is not None:
        _raise_lifespan_outcome(outcome, cleanup_error)


@dataclass(frozen=True, slots=True)
class ApplicationResources:
    """请求处理期间借用的应用级资源"""

    settings: Settings
    skills: SkillLibrary
    model_http_client: httpx.AsyncClient
    model_http_transport: ModelTransport
    attachments: AttachmentService
    database: Database  # 业务仓储使用的连接池
    components_database: Database
    redis_runtime: Redis
    agent_persistence: AgentPersistence
    agent_subagents: dict[str, SubagentSettings]
    tinkerfin: TinkerFin
    tracer: Tracer
    history_queries: HistoryQueryAdmission
    messaging: Messaging
    notifications: Notifications
    gateway: Gateway
    conversation_channel: AgUiChannel
    sandbox_manager: OpenSandboxManager[str]
    conversation_trace: ConversationTraceCoordinator
    conversation_titles: ConversationTitles
    readiness: ReadinessService
    automation: Automation


def build_lifespan():
    """构造数据库、Redis 与 Sandbox 的 FastAPI 生命周期"""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        settings = await asyncio.to_thread(get_settings)
        async with setup_logging(settings):
            stack = AsyncExitStack()
            outcome = _LifespanOutcome()
            resources_published = False
            try:
                logger.info("服务器已加载")
                logger.info(
                    "认证访问令牌固定有效期：%s 秒",
                    settings.auth_token_expire_seconds,
                )
                databases: list[Database] = []
                for database_settings in (
                    settings.business_database,
                    settings.components_database,
                ):
                    resource = Database(
                        database_settings.url,
                        echo=database_settings.echo,
                        pool_size=database_settings.pool_size,
                        max_overflow=database_settings.max_overflow,
                        pool_recycle=database_settings.pool_recycle,
                    )
                    await _enter_lifespan_context(stack, outcome, resource)
                    databases.append(resource)
                database, components_database = databases
                total_capacity = 2 * (
                    settings.database_pool_size + settings.database_max_overflow
                )
                for resource in databases:
                    await resource.verify_connection_budget(
                        total_pool_capacity=total_capacity,
                        configured_budget=settings.database_connection_budget,
                        management_reserve=settings.database_management_connection_reserve,
                    )
                logger.info(
                    "MySQL 连接预算已验证：双池合计=%s，预算=%s，管理保留=%s",
                    total_capacity,
                    settings.database_connection_budget,
                    settings.database_management_connection_reserve,
                )
                attachment_storage = await _enter_lifespan_context(
                    stack, outcome, MinioAttachmentStorage.open(settings.s3_storage)
                )
                await attachment_storage.initialize()
                redis_runtime_settings = settings.redis_runtime
                redis_runtime = create_redis_client(redis_runtime_settings)
                stack.push_async_callback(redis_runtime.aclose)
                http_client = await _enter_lifespan_context(
                    stack, outcome, httpx.AsyncClient(trust_env=False)
                )
                model_http_transport = ModelTransport()
                model_http_client = await _enter_lifespan_context(
                    stack,
                    outcome,
                    httpx.AsyncClient(
                        transport=model_http_transport,
                        trust_env=False,
                        follow_redirects=False,
                        timeout=600,
                    ),
                )
                if not await cast(Awaitable[bool], redis_runtime.ping()):
                    raise RuntimeError("Redis PING 未返回成功")

                # 会话、轨迹和调度共用变化通知，生产者关闭后再归还通知连接
                notifications = await _enter_lifespan_context(
                    stack,
                    outcome,
                    Notifications(
                        backend=NotificationRedisBackend(
                            redis_runtime, channel=NOTIFICATIONS_CHANNEL
                        )
                    ),
                )

                persistence = await _enter_lifespan_context(
                    stack,
                    outcome,
                    AgentPersistence(
                        components_database.engine, redis_runtime_settings
                    ),
                )
                trace_store = SqlAlchemyTraceStore(
                    components_database.engine, notifications=notifications
                )
                # 接收请求前检查轨迹存储，数据库连接仍由应用统一管理
                await trace_store.setup()
                tracer = Tracer(
                    projections=(
                        ConversationFailureProjection(),
                        TodoGroupProjection(),
                    ),
                    store=trace_store,
                    capture_policy=CapturePolicy.public_history(
                        include_error_messages=True
                    ),
                )
                history_queries = HistoryQueryAdmission()
                # 为会话记录运行轨迹；轨迹写入失败时中止运行
                # 保留有长度限制的错误摘要，省略模型的内部推理内容
                tinkerfin = TinkerFin(
                    checkpointer=persistence.checkpointer, store=persistence.store
                ).with_observer(tracer)
                sandbox_settings = settings.sandbox
                sandbox_manager = await _enter_lifespan_context(
                    stack,
                    outcome,
                    OpenSandboxManager[str](
                        client=OpenSandboxClient(
                            connection_config=ConnectionConfig(
                                domain=sandbox_settings.domain,
                                protocol=sandbox_settings.protocol,
                                api_key=(
                                    None
                                    if sandbox_settings.api_key is None
                                    else sandbox_settings.api_key.get_secret_value()
                                ),
                            ),
                            config=OpenSandboxConfig(
                                # 用户工作区跨会话保留，由明确的清理操作结束生命周期
                                ttl=None,
                                resource={
                                    "cpu": f"{sandbox_settings.cpu:g}",
                                    "memory": f"{sandbox_settings.memory_mib}Mi",
                                },
                                workspace_root=sandbox_settings.workspace_root,
                                warm_pool_size=sandbox_settings.warm_pool_size,
                            ),
                        ),
                        key_resolver=lambda owner: owner,
                        # 沙箱分配记录写入组件库，连接池由应用在组件关闭后释放
                        state=SQLAlchemyOpenSandboxState(
                            engine=components_database.engine,
                            namespace=sandbox_settings.state_namespace,
                        ),
                        warm_pool_size=sandbox_settings.warm_pool_size,
                        fail_on_startup_warmup_error=True,
                        notifications=notifications,
                        # 已确认的沙箱与预热容量变化复用应用日志输出
                        observers=(SandboxEventLogger(),),
                    ),
                )
                conversation_titles = ConversationTitles(
                    database=database,
                    notifications=notifications,
                    http_client=model_http_client,
                    http_transport=model_http_transport,
                )
                # 先关闭聊天生产者，再结算标题，最后释放模型客户端与数据库
                stack.push_async_callback(conversation_titles.aclose)
                messaging_backend = RedisBackend(
                    redis_runtime,
                    key_prefix=MESSAGING_KEY_PREFIX,
                    limits=_STUDIO_MESSAGING_LIMITS,
                    # Messaging 只承担短期断线续播；长期正文由 Trace 提供
                    retention_policy=_STUDIO_MESSAGING_RETENTION,
                )
                messaging = Messaging(backend=messaging_backend)
                (
                    channel,
                    gateway,
                    conversation_trace,
                ) = await _enter_conversation_resources(
                    stack,
                    outcome,
                    messaging=messaging,
                    database=database,
                    notifications=notifications,
                    tracer=tracer,
                )
                agent_subagents = await load_subagents()
                automation_store = SqlAlchemyAutomationStore(
                    components_database.engine, notifications=notifications
                )
                stack.push_async_callback(automation_store.close)
                automation = Automation(
                    namespace="studio_automation", store=automation_store
                )

                async def check_automation() -> None:
                    await automation_worker.check_ready()

                skill_archives = SkillArchiveReader()
                skill_github = GitHubSkillImporter(http_client, skill_archives)
                skill_sources = SkillSources(
                    [
                        GitHubSkillSource(source, http_client)
                        if source.kind == "github"
                        else ClawHubSkillSource(
                            source, http_client, skill_archives, skill_github
                        )
                        for source in settings.skill_sources
                    ]
                )
                attachments = AttachmentService(
                    database, attachment_storage, notifications=notifications
                )
                resources = ApplicationResources(
                    settings=settings,
                    skills=SkillLibrary(
                        database,
                        SkillContentStore(tinkerfin),
                        sources=skill_sources,
                        github=skill_github,
                        archives=skill_archives,
                        attachments=attachments,
                        notifications=notifications,
                    ),
                    automation=automation,
                    model_http_client=model_http_client,
                    model_http_transport=model_http_transport,
                    attachments=attachments,
                    database=database,
                    components_database=components_database,
                    redis_runtime=redis_runtime,
                    agent_persistence=persistence,
                    agent_subagents=agent_subagents,
                    tinkerfin=tinkerfin,
                    tracer=tracer,
                    history_queries=history_queries,
                    messaging=messaging,
                    notifications=notifications,
                    gateway=gateway,
                    conversation_channel=channel,
                    sandbox_manager=sandbox_manager,
                    conversation_trace=conversation_trace,
                    conversation_titles=conversation_titles,
                    readiness=ReadinessService(
                        business_database=database,
                        components_database=components_database,
                        redis=redis_runtime,
                        sandbox=settings.sandbox,
                        sandbox_ready=sandbox_manager.check_ready,
                        automation_ready=check_automation,
                        attachment_ready=attachment_storage.check_ready,
                        http_client=http_client,
                    ),
                )
                # 自动化沿用当前用户的会话资源范围，任务提交后由框架保存并校验
                automation.target(
                    "studio_agent",
                    StudioAutomationTarget(resources),
                    execution_namespace=lambda owner_id: f"ns_{int(owner_id)}",
                )
                automation_worker = await _enter_lifespan_context(
                    stack,
                    outcome,
                    automation.worker(on_interrupt=fail_interactive_execution),
                )
                await automation_worker.check_ready()
                application.state.resources = resources
                resources_published = True
                await application.state.resources.attachments.cleanup()
                yield
            except BaseException as error:  # noqa: BLE001 - 生命周期必须保留所有主因
                outcome.capture(error)
            finally:
                if resources_published:
                    del application.state.resources
                try:
                    await _settle_lifespan_stack(stack, outcome)
                except Exception:
                    logger.exception("服务生命周期失败")
                    raise

    return lifespan


def get_resources(application: FastAPI) -> ApplicationResources:
    """读取生命周期内已初始化的应用资源"""

    try:
        resources: ApplicationResources = application.state.resources
    except AttributeError as error:
        raise RuntimeError("应用资源尚未初始化") from error
    return resources
