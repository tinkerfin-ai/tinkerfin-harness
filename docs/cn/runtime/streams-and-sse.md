# 流与 SSE

[Runtime](index.md) · [English](../../en/runtime/streams-and-sse.md)

`open_run()` 和 `open_agui_run()` 返回单次消费的惰性对象流。自行消费时使用 `aclosing`，确保提前退出也会关闭流。

```python
from contextlib import aclosing

stream = runtime.open_run(
    thread_id=thread_id,
    run_id=run_id,
    input=graph_input,
)

async with aclosing(stream):
    async for part in stream:
        await handle(part)
```

每次拉取一个对象，正常耗尽会自动关闭流。

## 直接输出 SSE

`to_sse()` 是可选转换，将原生或 AG-UI 事件编码成 UTF-8 字节帧，可直接交给 HTTP 响应：

```python
from starlette.responses import StreamingResponse

return StreamingResponse(stream.to_sse(), media_type="text/event-stream")
```

使用 `sse-starlette` 的应用可以采用相同的响应内容：

```python
from sse_starlette import EventSourceResponse

return EventSourceResponse(stream.to_sse())
```

这两种消费方式二选一，同一个流只能消费一次。返回的字节无需再次编码；EventSourceResponse 可以额外发送自己的心跳注释。

流在耗尽、拉取异常、拉取期间取消或显式 `aclose()` 时关闭。首次拉取前或两次拉取之间发生 HTTP 发送失败时，清理依赖响应向流发出关闭通知；StreamingResponse 和 EventSourceResponse 并不保证所有发送失败都会通知。如果创建的响应内容最终没有交给 HTTP 响应，应主动关闭它。

确需发送前检查时，可使用 `prepare()`，不会消费事件：

```python
sse = stream.to_sse()
await sse.prepare(preflight=authorize)
return StreamingResponse(sse, media_type="text/event-stream")
```

原生 `to_sse()` 支持可选超时、内容映射和事件 ID 解析。AG-UI 的时间限制通过 `open_agui_run()` 的 `stream_timeout` 设置。

## 自定义原生 SSE

映射函数选择帧内容；`SsePayload.data` 使用文本，由编码器统一转换成 UTF-8：

```python
from tinkerfin import SsePayload


async def to_payload(part) -> SsePayload:
    return SsePayload(event="agent-part", data=part.model_dump_json())


sse = stream.to_sse(mapper=to_payload)
```

也可以不使用 `to_sse()`，直接消费原始对象并沿用自己的编码逻辑，或提供输出 EventSourceResponse 事件字典的生成器。Runtime 原始对象不会自动变成这种事件字典。

直接 SSE 不持久化或回放事件。需要按已提交序号回放时使用 [Messaging](../messaging/delivery-and-replay.md)：`await channel.open_sse(source)` 启动或附着运行，`subscription.to_sse()` 转换已有订阅。
