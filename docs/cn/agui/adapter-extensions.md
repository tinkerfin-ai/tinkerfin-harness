# 只使用 AG-UI 转换器

[interrupt 与恢复](interrupts-and-resume.md) · [English](../../en/agui/adapter-extensions.md)

如果应用已经自己创建和运行 LangGraph，只需要把原生 v2 数据转换成 AG-UI，可以单独使用 `tinkerfin-agui-adapter`。

## 安装

```bash
pip install tinkerfin-agui-adapter
```

转换器不创建 Graph、不调用模型、不读取 checkpoint，也不提供 HTTP 服务。

## 最简单的转换方式

```python
from contextlib import aclosing
from tinkerfin_agui_adapter import RunIdentity, astream_events


parts = graph.astream(
    graph_input,
    config,
    stream_mode=("messages", "tasks", "values"),
    version="v2",
    subgraphs=True,
)

events = astream_events(
    parts,
    identity=RunIdentity(threadId="thread-1", runId="run-1"),
)

async with aclosing(events):
    async for event in events:
        await send_event(event)
```

完整消费 `astream_events()` 时，中间事件前后会各有一个 `RUN_STARTED` 和主终止事件。异步上下文保证提前退出也会关闭来源，同一份 `parts` 不能交给多个消费者。

### 参数

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `parts` | 必填 | LangGraph v2 异步数据流 |
| `identity` | 必填 | 本次转换产生的主 thread 和 run 身份 |
| `expose_reasoning_events` | `False` | 是否产生支持的推理事件 |
| `expose_subagent_events` | `True` | 是否交付子 Agent 事件 |
| `prior_tool_call_ids` | `frozenset()` | 恢复前已经完整发送过的 scoped Tool ID |
| `private_state_keys` | `frozenset()` | 在已知公开投影边界排除的顶层 state channel |

独立转换器无法推断宿主的私有字段，因此 `private_state_keys` 需要显式提供，并且只过滤顶层
channel，不会递归删除嵌套同名业务字段。TinkerFin Plan Runtime 会自动提供自己的内部 key。

## 持久附件

`tinkerfin_contracts.media` 的 `Attachment` 包含 `id`、`name`、`mime_type`
和 `size_bytes`。调用 `content_block()` 会得到 LangChain 的 `image` 或 `file`
内容块，带有 `file_id`、`mime_type` 和 `extras.attachment`。消息保存该引用；宿主
负责访问授权，并通过 `TinkerFin().with_attachments(AttachmentSupport(read_content=...))`
在模型请求时提供有大小限制的文件内容。异步读取函数返回
`AttachmentContent(data=..., mime_type=...)`。目标模型必须支持对应格式；默认依据模型
profile 判断图片、音频、视频和 PDF 输入能力，也可通过
`supports_content(model, mime_type)` 显式判断。不支持的格式保留引用，供文件读取工具处理；
该能力不负责文件解析或视频抽帧。

`max_attachments` 默认每次请求读取最近 5 个受支持附件；`max_bytes` 默认限制编码前的
内容总量为 20 MiB，超出时在调用模型前抛出 `ValueError`。宿主还需在返回字节前限制
存储读取量。该策略作用于框架创建的图；外部编译图和远程智能体自行选择文件输入方式。

普通运行直接向 `runtime.open_agui_run(messages=...)` 提交标准用户消息，由 Runtime 校验并转换。宿主自行分配消息 ID 时，可用 `AgUiUserInput` 提前读取文字和附件引用，并替换已授权的附件描述。

自定义适配集成可使用 `tinkerfin_agui_adapter.media` 的
`user_message_to_langchain()` 将 AG-UI 用户消息转换为 LangChain 消息，或使用
`user_content_to_agui()` 将 LangChain 用户内容转换为 AG-UI。图片、音频、视频分别
使用 `type: "image"`、`"audio"`、`"video"`，其他文件使用 `"document"`。
附件片段的 `source` 为 `{"type": "url", "value": "attachment:<id>"}`，
`metadata` 保存附件描述；用户消息快照保留这一形态。

工具结果与助手快照通过 `attachments` 数组提供附件描述。助手流式输出使用 CUSTOM
事件 `tinkerfin.message.attachments`，值为
`{"messageId": "<scoped-id>", "attachments": [...]}`，位于对应的
TEXT_MESSAGE_START 与 TEXT_MESSAGE_END 之间。增量按附件 ID 合并，快照数组替换
已有描述。子 Agent 输出依据 `rawEvent.source` 归入对应调用。

`tinkerfin_agui_adapter.media` 的 `MessageAttachments` 校验该 CUSTOM 载荷。
包内提供 `contracts/message-attachments.schema.json`。附件描述不包含文件字节、密钥或临时下载地址。

`AttachmentToolCallResultEvent`、`AttachmentAssistantMessage` 和
`AttachmentToolMessage` 正式声明 `attachments` 字段；
`AttachmentMessagesSnapshotEvent` 校验并序列化包含附件的历史消息。
从通用 AG-UI 回放取得事件后，可调用 `parse_attachment_output_event(event)`，
将工具结果或消息快照校验为公开的附件类型。包内 `contracts/` 同时提供
`tool-call-result.schema.json`、`assistant-message.schema.json`、
`tool-message.schema.json` 和 `messages-snapshot.schema.json`。

## 编码成 SSE

```python
from tinkerfin_agui_adapter import encode_sse


async for event in events:
    frame = encode_sse(event, event_id=next_event_id())
    await send_text(frame)
```

`event_id` 可以是字符串或整数。`encode_sse()` 只编码一条事件，不负责保存、重试、回放或取消。

## 如果你要自己控制主生命周期

只有自定义编排器才需要逐条使用 `DeepAgentAgUiAdapter`：

```python
from tinkerfin_agui_adapter import (
    AgUiLifecycleEventFactory,
    DeepAgentAgUiAdapter,
)


lifecycle = AgUiLifecycleEventFactory()
identity = RunIdentity(threadId="thread-1", runId="run-1")
adapter = DeepAgentAgUiAdapter(identity=identity)

try:
    await send_event(lifecycle.started(identity=identity))
    async for part in parts:
        for event in adapter.process(part):
            await send_event(event)
    for event in adapter.finish():
        await send_event(event)
    await send_event(
        lifecycle.finished(
            identity=identity,
            outcome=adapter.main_outcome(),
        )
    )
except Exception:
    for event in adapter.abort():
        await send_event(event)
    await send_event(
        lifecycle.failed(
            identity=identity,
            message="Agent run failed",
            code="runtime_error",
        )
    )
finally:
    await parts.aclose()
```

`abort()` 只关闭文字、推理和 Tool 子生命周期；自定义编排器仍按上例唯一发送主 `RUN_ERROR`。

这里由你的代码负责“一个开始、一个终止”。一般应用优先使用 `astream_events()`，因为它已经处理了关闭、取消和失败顺序。

## 如果事件太碎

`micro_batch()` 可以合并连续的文字和工具参数增量，减少前端收到的事件数量，同时保留生命周期边界：

```python
from tinkerfin_agui_adapter import micro_batch


batched = micro_batch(events)
async for event in batched:
    await send_event(event)
```

## 如果需要保存自己的 ID

`ScopedIdCodec` 把 graph_namespace、对象类型和原始 ID 编成完整 ID：

```python
from tinkerfin_agui_adapter import ScopedIdCodec


codec = ScopedIdCodec()
public_id = codec.encode("tool", ("researcher",), "call-7")
kind, graph_namespace, raw_id = codec.decode(public_id)
```

不要截断 scoped ID，也不要只保存原始 Tool ID；不同 graph_namespace 中可能出现相同原始 ID。

## 解析框架扩展

Tool 审批 metadata 使用 `ToolReviewInterruptMetadata`，schema 固定为
`tinkerfin.deepagents.tool-review`：

```python
from tinkerfin_agui_adapter import parse_tool_review_interrupt


review = parse_tool_review_interrupt(persisted_interrupt)
print(review.tool_name, review.original_args.root)
```

解析器校验完整 interrupt、原生 action 分组、位置、决策策略、参数和 scoped Tool ID。字段冲突、
未知字段或来自客户端的 interrupt metadata 都不能作为可信恢复依据。

Deep Agents `task` 调用发布 `tinkerfin.subagent-provenance` 形状的
`SubagentProvenance`。`subagentInvocationId` 跨 resume 稳定，`requestRunId` 表示当前承载事件的
主请求；父 task Result 使用 `relatedSubagentInvocationId`。这些字段表达 Agent 嵌套，标准
AG-UI `parentRunId` 继续只表达分支和时间旅行谱系。

下一篇：[AG-UI 使用参考](api-reference.md)。
