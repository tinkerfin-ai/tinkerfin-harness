"""主会话中的技能查询和管理，复用页面的授权与提交入口"""

import json
from collections.abc import Awaitable
from typing import Annotated, Literal

from langchain_core.tools import BaseTool, InjectedToolArg, ToolException, tool
from pydantic import BaseModel, ConfigDict, Field

from tinkerfin.tools import ToolRuntime
from tinkerfin_studio.agent.tool_requests import tool_request_id
from tinkerfin_studio.api.errors import BusinessException
from tinkerfin_studio.skills.library import SkillLibrary
from tinkerfin_studio.skills.schemas import (
    ConfirmImportRequest,
    InstalledSkill,
    InstallSkillRequest,
    SkillCommandRequest,
    SkillEnabledRequest,
    SkillOperation,
    UpdateSkillRequest,
)


class _Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)


class _SourceArguments(_Arguments):
    """列出当前可查询的技能来源及来源标识"""


class _SearchArguments(_Arguments):
    """在指定来源浏览或搜索技能，返回可安装的固定发行标识"""

    source_id: str = Field(
        min_length=1, max_length=64, description="list_skill_sources 返回的来源 ID"
    )
    query: str = Field(
        default="", max_length=256, description="用途或名称关键词，空字符串表示浏览"
    )
    cursor: str | None = Field(
        default=None, max_length=8192, description="相同来源和查询返回的下一页游标"
    )


class _ListArguments(_Arguments):
    """查询当前用户已安装的技能及启用状态"""

    query: str = Field(
        default="",
        max_length=256,
        description="按名称、用途或作者查找，空字符串列出全部",
    )


class _InstalledTarget(_Arguments):
    kind: Literal["installed"] = Field(description="读取个人库中的已安装内容")
    installation_id: str = Field(
        min_length=1, max_length=36, description="list_skills 返回的个人安装 ID"
    )


class _CatalogTarget(_Arguments):
    kind: Literal["catalog"] = Field(description="读取来源目录中的技能内容")
    source_id: str = Field(
        min_length=1, max_length=64, description="list_skill_sources 返回的来源 ID"
    )
    skill_id: str = Field(
        min_length=1, max_length=256, description="search_skills 返回的来源内技能标识"
    )
    revision: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="搜索返回的固定发行标识，不传时读取当前发行",
    )


class _DetailArguments(_Arguments):
    """查看已安装技能或来源技能的说明与文件目录"""

    target: Annotated[
        _InstalledTarget | _CatalogTarget,
        Field(discriminator="kind", description="个人安装或来源目录中的技能身份"),
    ]


class _GitHubPreview(_Arguments):
    kind: Literal["github"] = Field(description="预览公开 GitHub 仓库或技能子目录")
    url: str = Field(
        min_length=1, max_length=2048, description="用户提供的公开 GitHub 地址"
    )


class _ZipPreview(_Arguments):
    kind: Literal["zip"] = Field(description="预览用户在当前会话上传或引用的 ZIP")
    attachment_id: str = Field(
        min_length=1,
        max_length=64,
        description="用户上传或明确引用的当前会话 ZIP 附件 ID",
    )


class _PreviewArguments(_Arguments):
    """预览 GitHub 地址或 ZIP 附件，返回候选技能与固定内容摘要，不执行安装"""

    source: Annotated[
        _GitHubPreview | _ZipPreview,
        Field(discriminator="kind", description="用户指定的 GitHub 地址或 ZIP 附件"),
    ]


class _ManageArguments(_Arguments):
    """按用户明确指令安装、更新或卸载技能；目标不明确时先查询，不猜测技能身份"""

    operation: SkillOperation = Field(description="安装、更新或卸载的目标及必要参数")
    runtime: Annotated[ToolRuntime, InjectedToolArg]


class _EnableArguments(_Arguments):
    """按用户明确指令启用或停用已安装技能，只影响后续新运行"""

    installation_id: str = Field(
        min_length=1, max_length=36, description="list_skills 返回的个人安装 ID"
    )
    enabled: bool = Field(
        strict=True, description="true 启用，false 停用；对下一次新运行生效"
    )
    runtime: Annotated[ToolRuntime, InjectedToolArg]


def build_skill_tools(
    library: SkillLibrary, *, user_id: int, thread_id: str
) -> tuple[BaseTool, ...]:
    """为主会话绑定可信用户和会话，模型不能覆盖身份或业务操作标识

    Args:
        library: 页面共用的技能服务，只借用应用资源
        user_id: 已认证且已校验会话归属的用户 ID
        thread_id: 当前会话，用于检查 ZIP 附件的引用范围

    Returns:
        查询和预览工具，以及 manage_skill、set_skill_enabled 两个管理工具
    """

    async def result(
        operation: Awaitable[BaseModel | list[InstalledSkill]],
        *,
        mutation: bool = False,
    ) -> str:
        try:
            value = await operation
        except BusinessException as error:
            raise ToolException(
                json.dumps(
                    {"code": int(error.error_code), "message": error.message},
                    ensure_ascii=False,
                )
            ) from error
        payload = (
            value.model_dump(mode="json")
            if isinstance(value, BaseModel)
            else [item.model_dump(mode="json") for item in value]
        )
        return json.dumps(
            {"result": payload, "effective_from": "next_run"} if mutation else payload,
            ensure_ascii=False,
        )

    @tool(
        args_schema=_SourceArguments,
        parse_docstring=True,
        error_on_invalid_docstring=True,
    )
    async def list_skill_sources() -> str:
        """列出当前可查询的技能来源及来源标识

        Returns:
            来源 ID、名称与公开地址，不包含凭据
        """
        return json.dumps(
            [source.model_dump(mode="json") for source in library.sources()],
            ensure_ascii=False,
        )

    @tool(
        args_schema=_SearchArguments,
        parse_docstring=True,
        error_on_invalid_docstring=True,
    )
    async def search_skills(
        source_id: str, query: str = "", cursor: str | None = None
    ) -> str:
        """在指定来源浏览或搜索技能，不安装或修改个人库

        Args:
            source_id: list_skill_sources 返回的来源 ID
            query: 技能用途或名称关键词，空字符串表示浏览
            cursor: 相同来源和查询的上一页游标

        Returns:
            技能候选、固定发行标识与下一页游标

        Raises:
            ToolException: 来源不存在、暂不可用或受到限流
        """
        return await result(library.search(source_id, query=query, cursor=cursor))

    @tool(
        args_schema=_ListArguments,
        parse_docstring=True,
        error_on_invalid_docstring=True,
    )
    async def list_skills(query: str = "") -> str:
        """查询当前用户已安装的技能，用真实安装 ID 进行后续管理

        Args:
            query: 可选名称或用途关键词

        Returns:
            当前用户安装信息、来源类型和启用状态

        Raises:
            ToolException: 技能查询不可用
        """
        return await result(library.list(user_id, query=query))

    @tool(
        args_schema=_DetailArguments,
        parse_docstring=True,
        error_on_invalid_docstring=True,
    )
    async def get_skill(target: _InstalledTarget | _CatalogTarget) -> str:
        """查看已安装技能或来源技能的说明和文件目录

        Args:
            target: 已安装 ID，或来源 ID 与来源内技能 ID

        Returns:
            只读技能说明；目录技能还返回可安装的固定发行标识

        Raises:
            ToolException: 技能不存在、无权访问或内容不可用
        """
        if target.kind == "installed":
            return await result(library.detail(user_id, target.installation_id))
        return await result(
            library.remote_detail(target.source_id, target.skill_id, target.revision)
        )

    @tool(
        args_schema=_PreviewArguments,
        parse_docstring=True,
        error_on_invalid_docstring=True,
    )
    async def preview_skills(source: _GitHubPreview | _ZipPreview) -> str:
        """预览 GitHub 或用户指定的 ZIP 附件，不安装、不执行技能代码

        Args:
            source: 公开 GitHub 地址，或当前会话的 ZIP 附件 ID

        Returns:
            预览 ID 与候选技能；安装或 ZIP 更新时使用返回的内容摘要

        Raises:
            ToolException: 地址、文件、权限或技能包不符合要求
        """
        if source.kind == "github":
            return await result(library.preview_github(user_id, source.url))
        return await result(
            library.preview_attachment(user_id, thread_id, source.attachment_id)
        )

    @tool(
        args_schema=_ManageArguments,
        parse_docstring=True,
        error_on_invalid_docstring=True,
    )
    async def manage_skill(operation: SkillOperation, runtime: ToolRuntime) -> str:
        """按用户明确指令安装、更新或卸载技能，成功后从下一次新运行生效

        安装使用搜索或预览返回的标识，不猜测发行号；GitHub 和 ZIP 先预览。
        更新自动按原来源路由，ZIP 必须提供新预览中的同名候选。
        卸载保留已开始运行引用的内容，更新保留安装 ID 和启用状态。

        Args:
            operation: install、update 或 uninstall 对应的完整参数

        Returns:
            已提交操作的真实结果及生效范围；相同调用重放返回原结果

        Raises:
            ToolException: 目标不明确、无权访问、来源失败、内容冲突或缺少调用身份
        """
        request_id = tool_request_id(runtime, user_id=user_id, operation="manage_skill")
        if operation.action == "install":
            target = operation.target
            if target.kind == "catalog":
                return await result(
                    library.install(
                        user_id,
                        InstallSkillRequest(
                            request_id=request_id,
                            source_id=target.source_id,
                            skill_id=target.skill_id,
                            revision=target.revision,
                        ),
                    ),
                    mutation=True,
                )
            return await result(
                library.confirm(
                    user_id,
                    target.draft_id,
                    ConfirmImportRequest(request_id=request_id, digests=target.digests),
                ),
                mutation=True,
            )
        if operation.action == "update":
            return await result(
                library.update(
                    user_id,
                    operation.installation_id,
                    UpdateSkillRequest(
                        request_id=request_id, replacement=operation.replacement
                    ),
                ),
                mutation=True,
            )
        return await result(
            library.uninstall(
                user_id,
                operation.installation_id,
                SkillCommandRequest(request_id=request_id),
            ),
            mutation=True,
        )

    @tool(
        args_schema=_EnableArguments,
        parse_docstring=True,
        error_on_invalid_docstring=True,
    )
    async def set_skill_enabled(
        installation_id: str, enabled: bool, runtime: ToolRuntime
    ) -> str:
        """启用或停用本人已安装的技能，不改变当前运行快照

        Args:
            installation_id: list_skills 返回的明确安装 ID
            enabled: true 表示启用，false 表示停用

        Returns:
            已提交的启用状态和生效范围

        Raises:
            ToolException: 安装不存在、无权访问或缺少稳定调用身份
        """
        request_id = tool_request_id(
            runtime, user_id=user_id, operation="set_skill_enabled"
        )
        return await result(
            library.set_enabled(
                user_id,
                installation_id,
                SkillEnabledRequest(request_id=request_id, enabled=enabled),
            ),
            mutation=True,
        )

    tools = (
        list_skill_sources,
        search_skills,
        list_skills,
        get_skill,
        preview_skills,
        manage_skill,
        set_skill_enabled,
    )
    for entry in tools:
        entry.handle_tool_error = True
    return tools
