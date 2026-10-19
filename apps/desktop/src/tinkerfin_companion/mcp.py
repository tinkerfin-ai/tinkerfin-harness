"""只提供本机已授权的 MCP 服务，程序启动受云端工具审批控制"""

import json
from typing import Annotated

from langchain_core.tools import BaseTool, ToolException, tool
from pydantic import Field, JsonValue

from tinkerfin_remote import ToolContextArg
from tinkerfin_sandbox import LocalError, LocalSession

from .configuration import McpServerSelection


def mcp_tools(servers: tuple[McpServerSelection, ...]) -> tuple[BaseTool, ...]:
    """把当前目录的本机服务授权绑定为工具，构建时不启动外部程序"""
    if not servers:
        return ()
    selected = {server.server_id: server for server in servers}

    def command(server: str) -> tuple[str, ...]:
        if server not in selected:
            raise ToolException("MCP 服务未在本机授权，请选择列表中的服务")
        return selected[server].command

    @tool(parse_docstring=True, error_on_invalid_docstring=True)
    async def list_mcp_servers() -> str:
        """列出当前本机工作区已授权的 MCP 服务。

        Returns:
            可供工具发现与调用使用的服务标识和名称
        """
        return json.dumps(
            [{"id": server.server_id, "name": server.label} for server in servers],
            ensure_ascii=False,
        )

    @tool(parse_docstring=True, error_on_invalid_docstring=True)
    async def list_mcp_tools(
        server: Annotated[str, Field(min_length=1, max_length=64)],
        runtime: ToolContextArg[LocalSession],
    ) -> str:
        """启动已授权的 MCP 服务并读取其工具目录，完成后关闭连接。

        Args:
            server: list_mcp_servers 返回的服务标识

        Returns:
            工具名称、用途和输入 Schema；内容由外部服务提供

        Raises:
            ToolException: 服务未授权、无法启动、超时或输出无效
        """
        try:
            catalog = await runtime.workspace.mcp_tools(command(server))
            return json.dumps(catalog, ensure_ascii=False)
        except (LocalError, ValueError) as error:
            raise ToolException(
                "MCP 工具目录不可用，请检查本机程序和文件权限"
            ) from error

    @tool(parse_docstring=True, error_on_invalid_docstring=True)
    async def call_mcp_tool(
        server: Annotated[str, Field(min_length=1, max_length=64)],
        name: Annotated[str, Field(min_length=1, max_length=128)],
        arguments: dict[str, JsonValue],
        runtime: ToolContextArg[LocalSession],
    ) -> str:
        """在本机文件与网络权限范围内调用一个 MCP 工具。

        Args:
            server: list_mcp_servers 返回的服务标识
            name: list_mcp_tools 返回的真实工具名称
            arguments: 按该工具输入 Schema 填写的 JSON 参数

        Returns:
            外部工具的内容和错误状态；连接不会保留到下一次调用

        Raises:
            ToolException: 服务未授权、无法启动、超时或输出无效；外部效果可能已发生
        """
        try:
            result = await runtime.workspace.call_mcp(command(server), name, arguments)
            return json.dumps(result, ensure_ascii=False)
        except (LocalError, ValueError) as error:
            raise ToolException(
                "MCP 调用未确认完成，请核对外部结果后再决定是否重试"
            ) from error

    for entry in (list_mcp_servers, list_mcp_tools, call_mcp_tool):
        entry.handle_tool_error = True
    return list_mcp_servers, list_mcp_tools, call_mcp_tool
