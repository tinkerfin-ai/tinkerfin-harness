# Streams and SSE

[Runtime](index.md) · [中文](../../cn/runtime/streams-and-sse.md)

`open_run()` and `open_agui_run()` return single-use, lazy object streams. Use
`aclosing` when consuming a stream yourself so early exit also closes it.

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

Each pull requests one item. Normal exhaustion closes the stream automatically.

## Direct SSE

`to_sse()` optionally encodes Native or AG-UI events as UTF-8 byte frames. Pass the
result directly to your HTTP response:

```python
from starlette.responses import StreamingResponse

return StreamingResponse(stream.to_sse(), media_type="text/event-stream")
```

Applications using `sse-starlette` can use the same body:

```python
from sse_starlette import EventSourceResponse

return EventSourceResponse(stream.to_sse())
```

These are alternative consumers: a stream can only be consumed once. Do not encode
the returned bytes again. EventSourceResponse may add its own heartbeat comments.

The body closes on exhaustion, pull failure, pull cancellation, or explicit `aclose()`.
If HTTP sending fails between pulls or before the first pull, cleanup depends on the
response notifying the body. StreamingResponse and EventSourceResponse do not guarantee
this for every send failure. Close a body you create but never hand to a response.

Optional checks before sending can use `prepare()` without consuming events:

```python
sse = stream.to_sse()
await sse.prepare(preflight=authorize)
return StreamingResponse(sse, media_type="text/event-stream")
```

Native `to_sse()` accepts an optional timeout, payload mapper, and event ID resolver.
AG-UI time limits use `stream_timeout` on `open_agui_run()`.

## Custom native SSE

A mapper selects frame content; `SsePayload.data` is text, which the encoder converts
to UTF-8 once:

```python
from tinkerfin import SsePayload


async def to_payload(part) -> SsePayload:
    return SsePayload(event="agent-part", data=part.model_dump_json())


sse = stream.to_sse(mapper=to_payload)
```

`to_sse()` is optional. You can consume the original objects and provide your own
encoding or an event generator yielding dictionaries accepted by EventSourceResponse.
Raw Runtime objects are not automatically EventSourceResponse event dictionaries.

Direct SSE does not persist or replay events. Use
[Messaging](../messaging/delivery-and-replay.md) for committed sequence replay:
`await channel.open_sse(source)` starts or attaches a run; `subscription.to_sse()`
encodes an existing subscription.
