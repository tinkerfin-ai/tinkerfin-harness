# 配置智能体

[Runtime](index.md) · [English](../../en/runtime/agent-configuration.md)

通过 `build()` 配置模型、工具、文件访问、任务委派和审批，取得可复用的
`AgentRuntime`，在应用中运行智能体。

```python
from tinkerfin import TinkerFin

runtime = (
    TinkerFin(checkpointer=checkpointer, store=store)
    .with_namespace("support")
    .build(
        name="support-agent",
        model="openai:gpt-5.4",
        tools=[search_orders],
        system_prompt="回答客户订单相关问题。",
    )
)
```

## 构建选项

| 能力 | 参数 |
| --- | --- |
| 模型与指令 | `model`、`system_prompt` |
| 工具与 middleware | `tools`、`middleware` |
| 子智能体 | `subagents` |
| 技能与记忆文件 | `skills`、`memory` |
| 文件系统 | `backend`、`permissions` |
| 工具审批 | `interrupt_on` |
| 结构化输出 | `response_format` |
| 状态与调用上下文 | `state_schema`、`context_schema` |

`model` 必填；checkpointer 和 Store 在 `TinkerFin(...)` 中配置。
传入的资源仍由应用管理。`build()` 只校验和保存配置，不执行 I/O。

使用 Store 的文件、记忆、技能和摘要 middleware 时，不要为 `StoreBackend` 单独传入
`store`，统一通过 `TinkerFin(store=...)` 提供。Runtime 为这些内置声明应用 namespace
隔离和异步文件访问，不修改调用方提供的配置对象。

使用 workspace 时，文件系统 middleware 和内置文件工具由 Runtime 提供，不可替换。
使用普通 backend 时，仅未配置有效 `permissions` 的角色允许自定义文件系统 middleware
或替换文件工具；冲突配置会在 `build()` 时抛出 `ValueError`。
任务委派通过 `subagents` 配置，工具审批通过 `interrupt_on` 配置，不可替换对应
middleware；`task` 和已配置远程智能体使用的委派工具名称也不可覆盖。

## 工具文件参数

workspace 提供文件路径解析器后，可用 `tinkerfin_contracts.FilePath` 标记自定义
工具的文件参数。Runtime 在审批前解析路径，并在执行前重新核验同一目标；模型消息
仍保留原始参数。恢复待审批或待执行的操作时，该角色的文件工具声明和路径解析能力
必须保持不变；发生变化后须重新发起操作，已有审批不能复用。

```python
from typing import Annotated

from langchain_core.tools import tool
from tinkerfin_contracts import FilePath


@tool
async def selected_files(files: dict[str, Annotated[str, FilePath("read")]]) -> str:
    """返回所选文件解析后的路径"""
    return "\n".join(files.values())


runtime = (
    TinkerFin()
    .with_namespace("reports")
    .build(model=model, backend=workspace, tools=[selected_files])
)
```

标记支持嵌套模型字段、字典值和列表元素。`ToolDescriptor` 的 schema 支持本地
JSON Pointer 引用、`properties`、`patternProperties`、`additionalProperties`、
`prefixItems` / `items` 和 `allOf` / `anyOf` / `oneOf`。同时适用的声明必须具有相同
访问类型和 `default_root`；一个属性匹配多个模式时只解析一次。条件分支等不支持
位置中的标记会在注册时被拒绝，通过引用间接声明也一样。

输入约束必须同时接受原始路径和解析后的规范绝对路径，且不能改变所选的文件访问
声明；不满足时停止执行。`FilePath("read", default_root=True)` 将省略或 null 的
参数解析为 workspace 根目录，输入 schema 也须允许对应的输入形式。

## 附件输入

绑定宿主提供的授权读取函数，框架在模型调用前选择附件：

```python
from tinkerfin import TinkerFin
from tinkerfin.media import AttachmentSupport

runtime = (
    TinkerFin()
    .with_namespace(user_namespace)
    .with_attachments(AttachmentSupport(read_content=read_authorized_attachment))
    .build(model=model, backend=workspace)
)
await runtime.ainvoke(
    thread_id=thread_id, run_id=run_id,
    input={"messages": [{"role": "user", "content": attachment_blocks}]},
)
```

异步函数 `read_authorized_attachment(attachment)` 接收 `Attachment`，校验当前权限、
有界读取并返回 `AttachmentContent(data=..., mime_type=...)`（从 `tinkerfin.media` 导入）。
文件缺失时抛出 `FileNotFoundError`；其他错误和取消继续传播。

默认优先选择最近的 5 个受支持附件（`max_attachments`），读取后纳入请求的原始字节不超过
20 MiB（`max_bytes`，不含 base64 开销）。缺少 reader、格式不支持或超限时保留引用，
正文需由文件工具读取。此预算不包括消息内已有的内联媒体，原件和历史保持不变。

格式支持取决于目标模型声明的图片、音频、视频和 PDF 能力；可用
`supports_content(model, mime_type)` 收窄范围。框架不负责文档解析或音视频转录。
编译后的子智能体和远程子智能体需单独配置附件访问。

## 持久化与审批

在智能体运行之外预置或读取数据时，使用已绑定 namespace 的 Store。应用负责 Store
的开启与关闭；应用操作和智能体工具在同一隔离范围内使用相对 namespace。

```python
from deepagents.backends import StoreBackend
from deepagents.backends.utils import create_file_data
from tinkerfin import SkillSource

scoped = TinkerFin(store=store, checkpointer=checkpointer).with_namespace("support")
await scoped.store.aput(
    ("files",),
    "/skills/reporting/SKILL.md",
    dict(
        create_file_data(
            "---\nname: reporting\ndescription: Prepare reports\n---\nVerify sources."
        )
    ),
)
runtime = scoped.build(
    model=model,
    skills=[SkillSource("/skills/")],
    backend=StoreBackend(namespace=lambda _: ("files",)),
)
```

技能来源指向包含技能子目录的父目录，每次模型调用前重新读取当前目录。
使用 `SkillSource("/skills/", names=("reporting",))` 限定提供的技能名称；
省略 `names` 加载全部，空元组不提供技能。目录内容更新会影响后续读取，包括恢复执行。
名称筛选只控制技能说明，不代替文件或执行权限。

工作区自行提供技能目录时，主智能体和自动创建的 `general-purpose` 会发现这些已准备好的来源。
显式声明的子智能体可从 `tinkerfin` 导入 `WorkspaceSkills`，再通过
`skills=[WorkspaceSkills(names=("reporting",))]` 按名称选择。实际目录由工作区提供，
选择结果仍受来源自身的名称限制。`WorkspaceSkills()` 选择工作区允许的全部技能；
显式子智能体没有声明选择时，不会获得这些技能。

可恢复的工具审批和 Plan 必须使用具体 checkpointer。checkpointer 已保存 thread 历史时，只提交新的用户消息。
托管 AG-UI 执行要求自定义 saver 实现异步 `aget_tuple`、`alist`、`aput` 和 `aput_writes`。请求准入会读取 checkpoint 历史，防止将已完成取消的 Run 身份用于不同输入。

```python
runtime = (
    TinkerFin(checkpointer=checkpointer)
    .with_namespace(namespace)
    .build(
        model=model,
        tools=[send_report],
        interrupt_on={"send_report": True},
    )
)
```

## 执行前审阅 Plan

在 builder 上启用 Plan，并在请求中选择 `mode="plan"`：

```python
runtime = (
    TinkerFin(checkpointer=checkpointer)
    .with_namespace(namespace)
    .with_plan(planner_model=model)
    .build(model=model, tools=tools)
)

result = await runtime.ainvoke(
    thread_id=thread_id,
    run_id=run_id,
    input=graph_input,
    mode="plan",
)
```

Plan 根据对话选择普通回复、澄清需求或生成计划草稿，使用已配置的工具、工作区、技能和权限规则。
backend 支持命令执行时，Plan 可以运行分析脚本；需要审批的工具仍须先获批准。
工具批准只授权本次操作，之后继续规划；只有批准当前草稿才开始执行该计划。
普通回复完成后仍保留 Plan 模式。
`mode="default"` 直接执行智能体。Plan 数据模型和审阅动作由 `tinkerfin.plan` 导出。

## 惰性 workspace 与工具

一次运行需要 Sandbox 或其他待准备文件系统时，把 `Workspace` 声明传给 backend：

```python
project = sandboxes.workspace("users/7", workspace_key="project-a")
runtime = (
    TinkerFin()
    .with_namespace("projects/project-a")
    .build(
        model=model,
        backend=project,
    )
)
```

Sandbox 工作区的首个 key 选择物理所有者，`workspace_key` 选择其中的项目。Runtime `namespace`
限定逻辑持久化范围，不改变这两个选择。每次已准入的运行都有独立进程和网络，清理时停止它们，并保留
项目文件、HOME、缓存和依赖环境。运行外通过 `async with project.open() as files` 访问项目；
`await project.delete()` 只停止并删除该项目。部署和所有权要求见 [Sandbox 指南](../sandbox/index.md)。

需要同时使用默认工作区和远程电脑时，将已认证设备的工作区作为具名资源绑定：

```python
computer = remote.endpoint(
    namespace=authenticated_namespace, target=authenticated_device
).workspace(authorization=authorized_workspace_token)
runtime = configured.build(
    model=model,
    backend=project,
    workspaces={"computer": computer},
    interrupt_on={"computer_write_file": True, "computer_execute": True},
)
await runtime.ainvoke(
    thread_id=thread_id,
    run_id=run_id,
    input={"messages": [{"role": "user", "content": "读取我电脑上的报告"}]},
)
```

宿主负责认证、配置 checkpointer，以及应用级 `RemoteTools` 资源和连接传输。
Runtime 在调用 `computer_tools` 发现工具时才准备电脑资源，开放带前缀的工具，并在运行完成、
中断或取消后释放借用。默认文件工具和记忆路由继续使用 `project`。
声明式子智能体与通用子智能体继承这些资源；已编译与远程子智能体自行配置。

工作区名称保留对应工具前缀，不能遮蔽内置工具，名称之间也不得有重叠前缀。辅助工作区的文件必须保存在 Graph 状态之外，
不能使用 `StateBackend`。`permissions` 只作用于默认 backend，辅助资源由提供方执行自己的访问规则；
审批配置使用带前缀的工具名。审批恢复会核对原资源身份、工具声明和文件目标，目标改变时必须重新发起操作。

工具在执行前定义，通过参数注解取得本次运行信息：

```python
from langchain_core.tools import tool

from tinkerfin.tools import ToolRuntime
from tinkerfin_sandbox import RootedOpenSandboxBackend


@tool
async def read_report(runtime: ToolRuntime[None, RootedOpenSandboxBackend]) -> str:
    """Read the report in this run's workspace."""
    content = await runtime.workspace.aread_bytes("/report.txt", max_bytes=64 * 1024)
    return content.decode("utf-8")


runtime = (
    TinkerFin()
    .with_namespace("projects/project-a")
    .build(
        model=model,
        tools=[read_report],
        backend=project,
    )
)
```

`ToolRuntime[ContextT, WorkspaceT, StateT]` 保留原始业务 `context`，并提供本次运行的
`identity` 和借用的 `workspace`。`StateT` 默认为 `DeepAgentState`，runtime 参数不会出现在
模型看到的工具参数中。未配置工作区或运行关闭后访问 `workspace` 会抛出
`TinkerFinLifecycleError`；工具不得关闭工作区，也不得持有它执行超出本次运行的后台任务。

## 子智能体

自动提供的 `general-purpose` 子智能体继承主智能体的模型、工具、技能、权限和审批规则。
其他角色通过 `tinkerfin.subagents.SubAgent` 声明：省略的模型、工具、权限和审批规则继承自
主智能体，技能需显式配置；显式工具列表替换主智能体声明的工具。
工作区准备时提供的工具会加入每个由框架创建的角色，并遵循该角色的审批规则。
名称重复（包括与内置文件、委派工具重名）会在模型运行前报错。

compiled 和 remote subagent 不继承工作区或附件访问权限，其作者自行选择文件输入方式；
框架创建的图可以使用自身 Runtime 配置的附件策略。注册的 compiled subagent 继承
Runtime 的 checkpointer 和 Store，不应另行固定 checkpoint 坐标。需要智能体维护任务清单时，
显式添加 `TodoListMiddleware`。

## 直接使用 Graph

高级调用方可以构建异步 Graph：

```python
from tinkerfin.deep_agent import create_graph

graph = await create_graph(runtime)
result = await graph.ainvoke(graph_input, config=config)
```

直接 Graph 的执行和清理由调用方负责。它不会创建受管 Runtime 观察、AG-UI 事件、Messaging 投递或每次运行的 workspace 准备。配置了 `backend=workspace` 的 Runtime 必须使用受管执行入口；工具访问工作区和运行身份也需要受管执行。

下一步：[流与 SSE](streams-and-sse.md)。
