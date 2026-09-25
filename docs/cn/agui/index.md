# AG-UI

[文档首页](../index.md) · [English](../../en/agui/index.md)

AG-UI 将智能体文字、工具调用、状态、审批和结果表示为前端事件。
`AgentRuntime` 负责转换并管理主事件生命周期。

## 安装

```bash
pip install "tinkerfin[agui]" langchain-openai
```

使用其他模型提供方时，替换对应的模型包。

## 打开事件流

```python
from contextlib import aclosing

from tinkerfin import TinkerFin

runtime = (
    TinkerFin(checkpointer=checkpointer)
    .with_namespace("customer-1")
    .build(model="openai:gpt-5.4", tools=tools)
)

events = runtime.open_agui_run(
    thread_id="conversation-1",
    run_id="request-1",
    messages=[{"id": "message-1", "role": "user", "content": "你好"}],
)

async with aclosing(events):
    async for event in events:
        await send_event(event)
```

应用决定 Runtime namespace，并负责 thread 与 run ID 的认证和授权。消息必须带最终且不重复的 ID。checkpointer 已保存 thread 历史时，只提交新的用户消息。

`open_agui_run()` 必须且只能提供 `messages`、原生 `input` 或 `resume` 中的一项。资源按需准备；只有预检或消费流时才会打开模型、Graph、Sandbox 和 observer session。

## HTTP 与 SSE

HTTP 层负责认证用户、选择 namespace、校验请求 ID，并映射模型、mode 等产品设置。客户端传来的工具描述不能授予工具执行权限。

直接输出 SSE：

```python
from starlette.responses import StreamingResponse

return StreamingResponse(events.to_sse(), media_type="text/event-stream")
```

需要持久投递和断线回放时，把事件流交给 Messaging：

```python
body = await channel.open_sse(events, after=last_event_id)
```

返回内容是 UTF-8 SSE 字节。EventSourceResponse 用法及 HTTP 关闭约束见[流与 SSE](../runtime/streams-and-sse.md)。

## 恢复审批

`AgUiResumeRequest` 只提交客户端决定。Runtime 从权威 checkpoint 重新读取 pending interrupt，校验完整批次后再继续执行。

```python
from tinkerfin import AgUiResumeRequest

events = runtime.open_agui_run(
    thread_id="conversation-1",
    run_id="request-2",
    resume=AgUiResumeRequest(entries=tuple(resume_entries)),
    parent_run_id=parent_run_id,
    on_resume_saved=record_receipt_idempotently,
    on_resume_not_saved=release_claim_idempotently,
)
```

`on_resume_saved` 接收不可变的 `AgUiResumeReceipt`，其中包含已保存的公开回复摘要。重试会交付相等的回执，应使用其不透明的 `receipt_id` 幂等结算。只有请求尚未持久保存时才会调用 `on_resume_not_saved`。取消整个待审批批次不会执行任何已审阅工具，也不会调用 `on_resume_saved`。

## 事件身份

主生命周期事件使用调用方提供的 `thread_id` 和 `run_id`。子智能体和工具 ID 包含完整 Graph 位置。`graph_namespace` 表示执行位置，Runtime namespace 仍是业务隔离范围。

每次运行只产生一个主开始和一个主终态。关闭或取消流时，也会关闭尚未结束的文字、推理和工具子生命周期。

## 后续阅读

- [事件](events.md)
- [Interrupt 与恢复](interrupts-and-resume.md)
- [Adapter 扩展](adapter-extensions.md)
- [AG-UI API](api-reference.md)
