"""独占沙箱中的程序绘图、中文 HTML 渲染和实际附件交付"""

import io
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from opensandbox.config import ConnectionConfig
from PIL import Image
from pydantic import SecretStr
from tests.support.docker_services import (
    OpenSandboxDockerRuntime,
    OpenSandboxTestService,
)

from tinkerfin import TinkerFin
from tinkerfin_automation import Automation
from tinkerfin_sandbox import OpenSandboxClient, OpenSandboxConfig, OpenSandboxManager
from tinkerfin_studio.agent import runtime as runtime_module
from tinkerfin_studio.models.schemas import AgentModelConfig
from tinkerfin_studio.resources import ApplicationResources
from tinkerfin_studio.skills.schemas import SkillSnapshotPayload

pytestmark = [pytest.mark.docker_integration, pytest.mark.opensandbox_e2e]

HTML = """<!doctype html><meta charset="utf-8"><style>
body { margin:0; width:720px; height:1200px; background:#f3f1e9; color:#182b32; font-family:"WenQuanYi Zen Hei",sans-serif; }
main { padding:64px; } h1 { font-size:48px; } p { font-size:28px; line-height:1.8; }
footer { position:absolute; top:1100px; left:64px; font-size:24px; }
</style><main><h1>季度经营回顾</h1><p>营收稳步增长<br>客户满意度持续提升</p></main><footer>下一步：优化客户体验</footer>"""
CHART = """import matplotlib
matplotlib.use('Agg')
from matplotlib import pyplot as plt
plt.rcParams['font.family'] = 'WenQuanYi Zen Hei'
fig, ax = plt.subplots(figsize=(7.2,4.8),dpi=100)
ax.bar(['一季度','二季度','三季度'],[12,19,24],color='#3158e6')
ax.set_title('季度收入'); fig.tight_layout(); fig.savefig('chart.png'); plt.close(fig)
"""


class DrawingModel(FakeMessagesListChatModel):
    """通过确定的任务选择真实工具，不依赖远端模型的随机回复"""

    drawing: str

    def bind_tools(self, tools, **kwargs):
        names = {tool.name for tool in tools}
        assert "generate_image" not in names
        assert {"capture_browser", "write_file", "execute", "deliver_file"} <= names
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        tool = next(
            (item for item in reversed(messages) if isinstance(item, ToolMessage)), None
        )
        if tool is None:
            name, args = (
                "write_file",
                {
                    "file_path": "/poster.html"
                    if self.drawing == "poster"
                    else "/chart.py",
                    "content": HTML if self.drawing == "poster" else CHART,
                },
            )
        elif tool.name == "write_file":
            if self.drawing == "poster":
                name, args = (
                    "capture_browser",
                    {
                        "file_path": "/poster.html",
                        "viewport_width": 720,
                        "viewport_height": 480,
                        "full_page": True,
                    },
                )
            else:
                name, args = "execute", {"command": "python chart.py"}
        elif tool.name in {"capture_browser", "execute"}:
            assert tool.status != "error", tool.content
            assert isinstance(tool.content, str)
            path = (
                json.loads(tool.content)["file_path"]
                if tool.name == "capture_browser"
                else "/chart.png"
            )
            name, args = (
                "deliver_file",
                {"file_path": path, "name": self.drawing + ".png"},
            )
        else:
            assert tool.name == "deliver_file" and tool.status != "error", tool.content
            return ChatResult(
                generations=[ChatGeneration(message=AIMessage(content="图片已交付"))]
            )
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {"id": f"{self.drawing}-{name}", "name": name, "args": args}
                        ],
                    )
                )
            ]
        )


@pytest.mark.parametrize("drawing", ["chart", "poster"])
async def test_programmatic_images_reach_downloadable_attachments(
    opensandbox_test_service: OpenSandboxTestService,
    opensandbox_docker_runtime: OpenSandboxDockerRuntime,
    attachments,
    skill_library,
    monkeypatch,
    tmp_path,
    drawing,
    use_server_proxy=True,
):
    client = OpenSandboxClient(
        connection_config=ConnectionConfig(
            domain=opensandbox_test_service.domain,
            api_key=opensandbox_test_service.api_key,
            use_server_proxy=use_server_proxy,
        ),
        config=OpenSandboxConfig(
            image=opensandbox_docker_runtime.image,
            workspace_root="/workspace",
            warm_pool_size=0,
            ttl=timedelta(minutes=20),
            metadata=opensandbox_test_service.sandbox_metadata,
            enable_capture_offload=True,
        ),
    )
    monkeypatch.setattr(
        runtime_module,
        "create_chat_model",
        lambda *args, **kwargs: DrawingModel(responses=[], drawing=drawing),
    )
    await attachments.create_collection(
        user_id=1,
        collection_id=drawing,
        purpose="execution",
        attachment_ids=(),
        configuration={},
    )
    async with OpenSandboxManager[str](client=client) as manager:
        resources = create_autospec(ApplicationResources, instance=True)
        resources.configure_mock(
            automation=Automation(namespace="image-test"),
            skills=skill_library,
            attachments=attachments,
            model_http_transport=None,
            model_http_client=None,
            agent_subagents={},
            tinkerfin=TinkerFin(checkpointer=InMemorySaver()),
            sandbox_manager=manager,
            settings=SimpleNamespace(),
        )
        runtime = runtime_module.build_automation_runtime(
            resources=resources,
            user_id=1,
            thread_id=drawing,
            execution_id=drawing,
            model_config=AgentModelConfig(
                model_id="text",
                display_name="Text",
                provider="openai",
                model_name="text-model",
                base_url="https://example.test",
                api_key=SecretStr("unused"),
                reasoning_enabled=False,
                image_support="unsupported",
            ),
            search_service=None,
            image_service=None,
            access_mode="full",
            skill_snapshot=SkillSnapshotPayload(
                directory_id="00000000-0000-0000-0000-000000000000", skills=()
            ),
        )
        try:
            result = await runtime.ainvoke(
                thread_id=drawing,
                run_id="render",
                input={"messages": [{"role": "user", "content": "制作并交付中文图片"}]},
            )
            messages = result["messages"]
            assert isinstance(messages, list)
            assert "图片已交付" in str(messages[-1])
            files = await attachments.list_collection(user_id=1, collection_id=drawing)
            assert len(files) == 1 and files[0].name == drawing + ".png"
            _, data = await attachments.read(
                files[0].id, user_id=1, collection_id=drawing
            )
            with Image.open(io.BytesIO(data)) as image:
                assert image.format == "PNG"
                assert image.size == (
                    (720, 1200) if drawing == "poster" else (720, 480)
                )
                image.verify()
            artifact = tmp_path / (drawing + ".png")
            artifact.write_bytes(data)
            print("rendered artifact:", artifact)
        finally:
            await manager.destroy("users/1", namespace=runtime.namespace)
