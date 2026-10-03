"""构建 Studio 会话 Runtime，声明用户工作区与业务工具"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from deepagents.backends.store import StoreBackend
from langchain.agents.middleware import TodoListMiddleware

from tinkerfin import AgentRuntime
from tinkerfin.media import AttachmentSupport
from tinkerfin.plan import PlanReviewAction
from tinkerfin.subagents import SubAgent
from tinkerfin_studio.agent.access import AccessMode, file_review_policy
from tinkerfin_studio.agent.plan_clarification import StudioPlanClarificationForm
from tinkerfin_studio.agent.plan_content import StudioMarkdownPlanContent
from tinkerfin_studio.agent.tool_policy import tool_execution_policy
from tinkerfin_studio.agent.tools import build_web_search_tool
from tinkerfin_studio.attachments.tools import build_attachment_tools
from tinkerfin_studio.attachments.workspace_tools import build_sandbox_attachment_tools
from tinkerfin_studio.automation.tools import build_automation_tools
from tinkerfin_studio.models.chat import create_chat_model
from tinkerfin_studio.models.schemas import AgentModelConfig
from tinkerfin_studio.services.service import ResolvedService
from tinkerfin_studio.skills.execution import (
    SkillsWorkspace,
    skill_source_path,
)
from tinkerfin_studio.skills.schemas import SkillSnapshotPayload
from tinkerfin_studio.skills.tools import build_skill_tools

if TYPE_CHECKING:
    from tinkerfin_studio.resources import ApplicationResources

_IMAGE_FILE_INSTRUCTIONS = """
数据图表、流程图和版式海报可用文件与 execute 工具制作，不需要 AI 图片生成服务。
图表可运行 Python / Pillow / Matplotlib；海报优先写含内嵌样式的工作区 HTML，
再用 capture_browser(file_path=..., viewport_width=..., viewport_height=..., full_page=True) 渲染 PNG。
脚本路径使用工作文件返回的 shell_path，不能把虚拟 file_path 当作命令中的绝对路径。
检查文件格式、尺寸和内容后再 deliver_file；有视觉输入时可读预览，没有视觉输入时不声称看过画面。
只有需要 AI 绘画的任务才调用 generate_image；该工具返回 files 列表，按用户要求交付所选格式。
未配置 AI 图片生成服务时如实说明该类任务限制，不把程序图表冒充写实照片或生成式编辑。
每个文件只有 deliver_file 成功后才算交付，不把工作文件路径当作已发布附件。
"""


_SYSTEM_PROMPT = """你是 TinkerFin Studio 的主 Agent。

复杂任务用 write_todos 跟踪，独立研究可用 task 委派。
需要工作文件时用 import_attachment 导入附件；引用丢失时用 list_attachments 查找。
生成工具只保存工作文件，按需读取、检查和修改；用 deliver_file 交付选定的工作文件。
工具成功后再确认结果，并简短回复；不交付无须给用户的中间文件。
工具失败先核实原因、调整方案，不原样重复调用。
"""


def _build_runtime(
    *,
    resources: ApplicationResources,
    user_id: int,
    thread_id: str,
    model_config: AgentModelConfig,
    search_service: ResolvedService | None,
    image_service: ResolvedService | None,
    access_mode: AccessMode,
    namespace: str,
    collection_id: str | None,
    plan_enabled: bool,
    skill_snapshot: SkillSnapshotPayload,
) -> AgentRuntime[None]:
    """为已授权会话或后台任务绑定执行能力，实际运行时准备用户工作区

    同一用户的会话与后台任务共用默认项目文件，每次执行结束时停止其进程
    业务逻辑范围分别保存会话与记录，不改变所选项目

    Args:
        resources: 请求期间借用的应用资源
        user_id: 已认证用户的数据库 ID，决定会话与记忆隔离范围
        thread_id: 已校验归属的会话 ID
        model_config: 已解密的聊天模型配置
        search_service: 本次运行可用的个人网页搜索服务
        image_service: 本次运行可用的个人图片生成服务
        access_mode: 脚本与文件写入的审批选择，不改变用户工作区范围
        namespace: 已授权业务运行的会话与记录范围
        collection_id: 后台执行的附件集合，普通会话不设置
        plan_enabled: 是否允许会话计划与人工交互
        skill_snapshot: 本次运行已固定的技能内容，恢复沿用原快照

    Returns:
        绑定用户 namespace、模型、Plan 和附件能力的 Runtime

    Raises:
        TypeError: 模型或业务配置类型不符合要求
        ValueError: 工作区、子智能体或模型配置无效
    """

    model = create_chat_model(
        model_config,
        http_async_transport=resources.model_http_transport,
        http_async_client=resources.model_http_client,
    )
    web_search = build_web_search_tool(search_service)

    attachment_tools = [
        *build_attachment_tools(
            service=resources.attachments,
            processor=resources.attachments.documents,
            user_id=user_id,
            thread_id=None if collection_id is not None else thread_id,
            collection_id=collection_id,
            image_service=image_service,
        ),
        *build_sandbox_attachment_tools(
            service=resources.attachments,
            user_id=user_id,
            thread_id=None if collection_id is not None else thread_id,
            collection_id=collection_id,
        ),
    ]

    attachment_support = AttachmentSupport(
        read_content=lambda attachment: resources.attachments.read_content(
            attachment,
            user_id=user_id,
            thread_id=None if collection_id is not None else thread_id,
            collection_id=collection_id,
        ),
    )
    skill_paths = [
        skill_source_path(skill_snapshot, skill) for skill in skill_snapshot.skills
    ]
    tool_registry = {web_search.name: web_search}
    subagents: list[SubAgent] = [
        {
            "name": "general-purpose",
            "description": "处理主 Agent 委派的资料整理、分析与文件任务",
            "system_prompt": "完成委派任务并返回结果；自动化任务的管理由主 Agent 处理。"
            + _IMAGE_FILE_INSTRUCTIONS,
            "interrupt_on": file_review_policy(access_mode),
            "middleware": tool_execution_policy(
                web_search_available=search_service is not None,
                image_generation_available=image_service is not None,
            ),
            "tools": [web_search, *attachment_tools],
            "skills": skill_paths,
        },
    ]
    subagents.extend(
        {
            "name": name,
            "description": definition.description,
            "system_prompt": definition.system_prompt + _IMAGE_FILE_INSTRUCTIONS,
            "interrupt_on": file_review_policy(access_mode),
            "middleware": tool_execution_policy(
                web_search_available=search_service is not None,
                image_generation_available=image_service is not None,
            ),
            "skills": [
                skill_source_path(skill_snapshot, skill)
                for skill in skill_snapshot.skills
                if skill.name in definition.skills
            ],
            "tools": [
                *(tool_registry[tool] for tool in definition.tools),
                *attachment_tools,
            ],
        }
        for name, definition in resources.agent_subagents.items()
    )
    conversation_tools = (
        build_automation_tools(
            resources,
            user_id=user_id,
            model_id=model_config.model_id,
            access_mode=access_mode,
        )
        if collection_id is None
        else ()
    )
    skill_tools = (
        build_skill_tools(resources.skills, user_id=user_id, thread_id=thread_id)
        if collection_id is None
        else ()
    )
    skill_instructions = (
        "\n用户明确要求时可用 manage_skill 安装、更新、卸载技能，set_skill_enabled 启用或停用技能。"
        "管理前通过 list_skills 或 search_skills 确定真实身份；来源不明确先 list_skill_sources。"
        "GitHub 或 ZIP 先用 preview_skills，多个候选只选择用户要求的项目。ZIP 附件只交给技能预览，不运行包内代码。"
        "目标明确时无需再次确认，指代不清先询问，不自动改用其他来源。"
        "仅按工具成功结果报告完成；安装、更新和状态变化只对下一次新运行生效，本轮及恢复继续使用原快照。\n"
        if skill_tools
        else ""
    )
    automation_instructions = (
        "\n用户明确要求时可直接创建、修改、暂停、启用、删除或立即运行自动化任务，无须再次确认。"
        "缺少时间、执行内容或任务指代不清时先询问；控制已有任务前先查询其 ID 和 revision。"
        "修改提交完整配置并保留用户未要求修改的字段；不要静默替换冲突版本或重复失败命令。"
        "创建保存独立完整指令，默认使用当前模型与文件审批选择，仅引用明确指定的附件。"
        "根据工具成功结果回报名称、日程和北京时间下次执行时间，可在侧栏自动化页面管理。"
        "立即运行只代表已提交；后台任务需要人工交互时会失败，不会自动批准。"
        "核对执行情况用 list_automation_runs，读取结果用 get_automation_run；任务 inputFiles 是参考文件，运行 outputFiles 才是产物。"
        "不能从下次执行时间或工作目录推断是否运行；查询失败或结果未就绪时如实说明。"
        "用户要求取回已有结果时用 deliver_automation_files，不重新运行任务或生成文件。"
        "时间统一按北京时间，工作日指周一至周五。"
        f"当前北京时间：{datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='minutes')}。\n"
        if conversation_tools
        else ""
    )
    configured = (
        resources.tinkerfin.with_compaction_tool()
        .with_namespace(namespace)
        .with_attachments(attachment_support)
    )
    if plan_enabled:
        configured = configured.with_plan(
            enabled=True,
            planner_model=model,
            clarification_schema=StudioPlanClarificationForm,
            content_schema=StudioMarkdownPlanContent,
            allowed_review_actions=(
                PlanReviewAction.APPROVE,
                PlanReviewAction.REJECT,
                PlanReviewAction.CANCEL,
                PlanReviewAction.RESPOND,
            ),
        )
    else:
        configured = configured.with_plan(enabled=False)
    workspace = resources.sandbox_manager.workspace(
        f"users/{user_id}",
        workspace_key="default",
        routes={"/memories/": StoreBackend(namespace=lambda _runtime: ("memories",))},
    )
    return configured.build(
        model=model,
        tools=[web_search, *attachment_tools, *conversation_tools, *skill_tools],
        system_prompt=_SYSTEM_PROMPT
        + _IMAGE_FILE_INSTRUCTIONS
        + automation_instructions
        + skill_instructions,
        middleware=(
            TodoListMiddleware(),
            *tool_execution_policy(
                web_search_available=search_service is not None,
                image_generation_available=image_service is not None,
            ),
        ),
        subagents=subagents,
        skills=skill_paths,
        backend=SkillsWorkspace(
            workspace,
            content=resources.skills.content,
            user_id=user_id,
            snapshot=skill_snapshot,
        )
        if skill_snapshot.skills
        else workspace,
        interrupt_on=file_review_policy(access_mode),
    )


def build_conversation_runtime(
    *,
    resources: ApplicationResources,
    user_id: int,
    thread_id: str,
    model_config: AgentModelConfig,
    search_service: ResolvedService | None,
    image_service: ResolvedService | None,
    skill_snapshot: SkillSnapshotPayload,
    access_mode: AccessMode = "full",
) -> AgentRuntime[None]:
    """绑定会话模型、用户工作区与文件审批选择，资源由运行时按需准备"""
    return _build_runtime(
        resources=resources,
        user_id=user_id,
        thread_id=thread_id,
        model_config=model_config,
        search_service=search_service,
        image_service=image_service,
        access_mode=access_mode,
        namespace=f"ns_{user_id}",
        collection_id=None,
        plan_enabled=True,
        skill_snapshot=skill_snapshot,
    )


def build_automation_runtime(
    *,
    resources: ApplicationResources,
    user_id: int,
    thread_id: str,
    execution_id: str,
    model_config: AgentModelConfig,
    search_service: ResolvedService | None,
    image_service: ResolvedService | None,
    access_mode: AccessMode,
    skill_snapshot: SkillSnapshotPayload,
) -> AgentRuntime[None]:
    """为独立后台执行绑定产物集合，沿用该用户的沙箱与长期记忆"""
    return _build_runtime(
        resources=resources,
        user_id=user_id,
        thread_id=thread_id,
        model_config=model_config,
        search_service=search_service,
        image_service=image_service,
        access_mode=access_mode,
        namespace=f"ns_{user_id}",
        collection_id=execution_id,
        plan_enabled=False,
        skill_snapshot=skill_snapshot,
    )


__all__ = ["build_automation_runtime", "build_conversation_runtime"]
