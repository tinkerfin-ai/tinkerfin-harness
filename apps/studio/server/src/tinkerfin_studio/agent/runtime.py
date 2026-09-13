"""构建 Studio 会话 Runtime，声明用户工作区与业务工具"""

from __future__ import annotations

from typing import TYPE_CHECKING

from deepagents.backends.store import StoreBackend
from langchain.agents.middleware import TodoListMiddleware

from tinkerfin import AgentRuntime
from tinkerfin.media import AttachmentSupport
from tinkerfin.plan import PlanReviewAction
from tinkerfin.subagents import SubAgent
from tinkerfin_studio.agent.access import AccessMode, file_review_policy
from tinkerfin_studio.agent.plan_clarification import StudioPlanClarificationForm
from tinkerfin_studio.agent.plan_content import StudioMarkdownPlanContent
from tinkerfin_studio.agent.tools import build_web_search_tool
from tinkerfin_studio.attachments.sandbox_tools import build_sandbox_attachment_tools
from tinkerfin_studio.attachments.tools import build_attachment_tools
from tinkerfin_studio.models.chat import create_chat_model
from tinkerfin_studio.models.schemas import AgentModelConfig

if TYPE_CHECKING:
    from tinkerfin_studio.resources import ApplicationResources

_SYSTEM_PROMPT = """你是 TinkerFin Studio 的主 Agent。

处理复杂任务时使用 write_todos 维护清单，
使用 task 委派适合的独立研究任务，
使用文件工具在用户 Sandbox 中读写结果。
附件引用中的 ID 可用于 read_attachment 或 view_image。
历史内容压缩后，先用 list_attachments 重新查找附件，不要假装已读原文件。
使用 create_file 或 generate_image 交付可下载结果，工具成功才表示文件存在。
工作区内已有文件用 deliver_file 交付；网页截图用 capture_browser。
"""


def _build_runtime(
    *,
    resources: ApplicationResources,
    user_id: int,
    thread_id: str,
    model_config: AgentModelConfig,
    image_model: AgentModelConfig | None,
    access_mode: AccessMode,
    namespace: str,
    collection_id: str | None,
    plan_enabled: bool,
) -> AgentRuntime[None]:
    """为已授权会话或后台任务绑定执行能力，实际运行时准备用户工作区

    Args:
        resources: 请求期间借用的应用资源
        user_id: 已认证用户的数据库 ID，决定会话与记忆隔离范围
        thread_id: 已校验归属的会话 ID
        model_config: 已解密的聊天模型配置
        image_model: 可选的图片生成模型配置
        access_mode: 写文件工具的审批选择，不改变用户工作区范围
        namespace: 已授权业务运行的隔离范围
        collection_id: 后台执行的附件集合，普通会话不设置
        plan_enabled: 是否允许会话计划与人工交互

    Returns:
        绑定用户 namespace、模型、Plan 和附件能力的 Runtime

    Raises:
        TypeError: 模型或业务配置类型不符合要求
        ValueError: 工作区、子智能体或模型配置无效
    """

    model = create_chat_model(
        model_config, http_async_client=resources.model_http_client
    )
    plan_model = (
        create_chat_model(
            model_config,
            reasoning_enabled=False,
            http_async_client=resources.model_http_client,
        )
        if plan_enabled
        and model_config.provider == "deepseek"
        and model_config.reasoning_enabled
        else model
    )
    api_key = resources.settings.tavily_api_key
    web_search = build_web_search_tool(
        None if api_key is None else api_key.get_secret_value()
    )

    attachment_tools = [
        *build_attachment_tools(
            service=resources.attachments,
            processor=resources.attachments.documents,
            user_id=user_id,
            thread_id=None if collection_id is not None else thread_id,
            collection_id=collection_id,
            image_model=image_model,
            supports_images=model_config.image_support == "supported",
            model_allowed_origins=resources.settings.model_allowed_origins,
        ),
        *build_sandbox_attachment_tools(
            service=resources.attachments,
            user_id=user_id,
            thread_id=None if collection_id is not None else thread_id,
            collection_id=collection_id,
        ),
    ]

    attachment_support = AttachmentSupport(
        read_content=lambda attachment: resources.attachments.read_image(
            attachment,
            user_id=user_id,
            thread_id=None if collection_id is not None else thread_id,
            collection_id=collection_id,
        ),
        supports_content=lambda candidate, mime_type: (
            mime_type.startswith("image/")
            and (candidate is model or candidate is plan_model)
            and model_config.image_support == "supported"
        ),
    )
    tool_registry = {web_search.name: web_search}
    subagents: list[SubAgent] = [
        {
            "name": name,
            "description": definition.description,
            "system_prompt": definition.system_prompt,
            "interrupt_on": file_review_policy(access_mode),
            "tools": [
                *(tool_registry[tool] for tool in definition.tools),
                *attachment_tools,
            ],
        }
        for name, definition in resources.agent_subagents.items()
    ]
    configured = resources.tinkerfin.with_namespace(namespace).with_attachments(
        attachment_support
    )
    if plan_enabled:
        configured = configured.with_plan(
            enabled=True,
            planner_model=plan_model,
            clarification_schema=StudioPlanClarificationForm,
            content_schema=StudioMarkdownPlanContent,
            allowed_review_actions=(
                PlanReviewAction.APPROVE,
                PlanReviewAction.REJECT,
                PlanReviewAction.CANCEL,
            ),
        )
    else:
        configured = configured.with_plan(enabled=False)
    return configured.build(
        model=model,
        tools=[web_search, *attachment_tools],
        system_prompt=_SYSTEM_PROMPT,
        middleware=(TodoListMiddleware(),),
        subagents=subagents,
        backend=resources.sandbox_manager.workspace(
            f"users/{user_id}",
            routes={
                "/memories/": StoreBackend(
                    namespace=lambda _runtime: (
                        ("memories",)
                        if collection_id is None
                        else ("users", str(user_id), "memories")
                    )
                )
            },
        ),
        interrupt_on=file_review_policy(access_mode),
    )


def build_conversation_runtime(
    *,
    resources: ApplicationResources,
    user_id: int,
    thread_id: str,
    model_config: AgentModelConfig,
    image_model: AgentModelConfig | None,
    access_mode: AccessMode = "full",
) -> AgentRuntime[None]:
    """绑定会话模型、用户工作区与文件审批选择，资源由运行时按需准备"""
    return _build_runtime(
        resources=resources,
        user_id=user_id,
        thread_id=thread_id,
        model_config=model_config,
        image_model=image_model,
        access_mode=access_mode,
        namespace=f"ns_{user_id}",
        collection_id=None,
        plan_enabled=True,
    )


def build_automation_runtime(
    *,
    resources: ApplicationResources,
    user_id: int,
    thread_id: str,
    execution_id: str,
    model_config: AgentModelConfig,
    image_model: AgentModelConfig | None,
    access_mode: AccessMode,
) -> AgentRuntime[None]:
    """绑定一次后台执行的文件集合；用户沙箱与记忆分别保持隔离"""
    return _build_runtime(
        resources=resources,
        user_id=user_id,
        thread_id=thread_id,
        model_config=model_config,
        image_model=image_model,
        access_mode=access_mode,
        namespace="studio_automation",
        collection_id=execution_id,
        plan_enabled=False,
    )


__all__ = ["build_automation_runtime", "build_conversation_runtime"]
