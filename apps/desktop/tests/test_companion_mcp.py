"""本机 MCP 程序选择与工具上下文，不创建本机 Agent"""

import json

from pydantic import BaseModel
from test_cloud import configuration, execution_request
from tool_context import tool_context

from tinkerfin_companion.configuration import McpServerSelection
from tinkerfin_companion.mcp import mcp_tools
from tinkerfin_sandbox import LocalWorkspace


async def test_mcp_program_uses_local_authorization_and_real_workspace(
    tmp_path, native_sandbox
):
    script = tmp_path / "mcp_service.py"
    script.write_text('''
from pathlib import Path
from mcp.server import MCPServer
Path('started').write_text('started')
server = MCPServer('authorized')
@server.tool()
def save(value: str) -> str:
    """保存明确传入的内容"""
    Path('result').write_text(value)
    return value
server.run()
''')
    selected = McpServerSelection(
        server_id="reports", label="报告服务", command=("python", str(script))
    )
    tools = mcp_tools((selected,))
    assert mcp_tools(()) == () and not (tmp_path / "started").exists()
    assert json.loads(await tools[0].ainvoke({})) == [
        {"id": "reports", "name": "报告服务"}
    ]
    schema_type = tools[2].tool_call_schema
    assert isinstance(schema_type, type) and issubclass(schema_type, BaseModel)
    assert "runtime" not in schema_type.model_json_schema()["properties"]
    request = execution_request(configuration(tmp_path))
    async with LocalWorkspace(tmp_path, sandbox_executable=native_sandbox) as workspace:
        async with workspace.prepare(request.identity) as prepared:
            with tool_context(prepared.workspace, request, "mcp-call") as context:
                denied = await tools[2].ainvoke(
                    {
                        "server": "unapproved",
                        "name": "save",
                        "arguments": {"value": "forbidden"},
                        "runtime": context,
                    }
                )
                assert "MCP 服务未在本机授权" in denied
                assert not (tmp_path / "started").exists()
                result = await tools[2].ainvoke(
                    {
                        "server": "reports",
                        "name": "save",
                        "arguments": {"value": "accepted"},
                        "runtime": context,
                    }
                )
                assert (
                    "accepted" in result
                    and (tmp_path / "result").read_text() == "accepted"
                )
