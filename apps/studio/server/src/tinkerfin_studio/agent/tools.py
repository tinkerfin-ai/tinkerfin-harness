"""Studio 提供给 Deep Agents 的网页搜索 Tool"""

from __future__ import annotations

import logging
from typing import Literal

from langchain_core.tools import BaseTool, ToolException, tool
from pydantic import BaseModel, Field, JsonValue, TypeAdapter

from tinkerfin_studio.services.http import SearchRequest, search_web
from tinkerfin_studio.services.service import ResolvedService

logger = logging.getLogger(__name__)


class WebSearchInput(BaseModel):
    """网页搜索 Tool 的模型可见输入"""

    query: str = Field(
        min_length=2, max_length=500, description="具体、明确的搜索关键词"
    )
    max_results: int = Field(default=5, ge=1, le=10, description="最多返回的结果数量")
    topic: Literal["general", "news"] = Field(
        default="general", description="普通网页或新闻主题"
    )


def build_web_search_tool(service: ResolvedService | None) -> BaseTool:
    """搜索工具使用本次运行绑定的个人服务"""

    @tool(
        "web_search",
        args_schema=WebSearchInput,
        response_format="content_and_artifact",
        parse_docstring=True,
        error_on_invalid_docstring=True,
    )
    async def web_search(
        query: str,
        max_results: int = 5,
        topic: Literal["general", "news"] = "general",
    ) -> tuple[str, dict[str, JsonValue]]:
        """搜索实时网页信息，返回标题、来源 URL 与内容摘要

        Args:
            query: 具体、明确的搜索关键词
            max_results: 最多返回的结果数量
            topic: 搜索主题，支持普通网页或新闻

        Returns:
            可供模型读取的 JSON 内容和结构化搜索结果

        Raises:
            ToolException: 本次运行未配置搜索服务或服务请求失败
        """
        if service is None:
            raise ToolException(
                '{"status":"error","error":"Web search is not configured"}'
            )
        request = WebSearchInput(query=query, max_results=max_results, topic=topic)
        try:
            result = await search_web(
                service,
                SearchRequest(
                    query=request.query,
                    max_results=request.max_results,
                    topic=request.topic,
                ),
            )
        except Exception as error:
            logger.warning("网页搜索失败: error_type=%s", type(error).__name__)
            raise ToolException(
                '{"status":"error","error":"Web search failed"}'
            ) from error
        return result.model_dump_json(exclude_none=True), TypeAdapter(
            dict[str, JsonValue]
        ).validate_python(result.model_dump(mode="json", exclude_none=True))

    web_search.handle_tool_error = True
    return web_search
