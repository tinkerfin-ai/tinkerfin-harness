# 投递、回放和 SSE

[Messaging 入门](index.md) · [English](../../en/messaging/delivery-and-replay.md)

## 启动或附着

```python
subscription = await channel.wrap(
    source,
    identity=identity,  # TinkerFin profile source 可省略
    after=0,
)
```

`wrap()` 返回前会原子决定：

- 没有该 run：当前调用成为 owner 并启动 source；
- 已有该 run：关闭未使用的候选 source，附着已有日志；
- 同一 thread 有另一个活跃 run：抛出 `RunAlreadyActive`；
- codec 与 channel 已绑定格式不同：抛出 `CodecMismatch`。

`run_id` 是同一 namespace 与线程内的运行幂等标识。Messaging 不读取或保存业务请求摘要；同一个 `RunIdentity` 的正文是否一致由应用校验。

## 直接获得 SSE

```python
from starlette.responses import StreamingResponse
from tinkerfin_messaging import parse_sse_event_id


body = await channel.open_sse(
    source,
    identity=identity,
    after=lambda: parse_sse_event_id(request.headers.get("Last-Event-ID")),
)
return StreamingResponse(body, media_type="text/event-stream")
```

`after` resolver 只调用一次，并且在 durable prepare 之前执行。`parse_sse_event_id()` 只接受
canonical 非负 ASCII 十进制值。返回的每帧使用提交序号作为 SSE `id`。

可选回调 `on_source_ready` 在请求 source 就绪后、producer 创建之前为新 owner 调用一次。
`on_delivery_not_started` 只在 source 未就绪且 attachment 未成立时调用；attachment 不调用两者。
返回 body 由调用方拥有，不再消费时必须关闭。

有效 attachment 成立后，Messaging 会关闭未打开的 single-use candidate source，调用方不能复用。

相同字节流也可交给 `EventSourceResponse(body)`。HTTP 关闭约束见[流与 SSE](../runtime/streams-and-sse.md)。

## 运行中主动发布消息

```python
from ag_ui.core import CustomEvent

await channel.publish(
    CustomEvent(name="report.progress", value={"completed": 3, "total": 10}),
    identity=identity,
    message_id="report-progress-3",
)
```

消息与源事件进入同一份持久化日志，已有订阅者实时接收，重连沿用同一个序号游标回放。目标身份的业务授权由宿主负责。

发布前 channel 必须已绑定 codec。AG-UI 在各 worker 上均使用 `messaging.agui_channel(name=...)` 创建同名频道。普通 codec 在首条源消息提交后接受发布；AG-UI 仅在目标主 `RUN_STARTED` 提交后接受 `CustomEvent`，主终态与关闭发布在同一提交中完成。子运行事件不改变主运行的发布状态。取消、结算或生产者所有权失效后，新发布抛出 `PublicationRejected`。

省略 `message_id` 时每次调用生成新 ID。相同 ID、相同内容返回仍在保留期内的原消息，包括运行已经结束的情况；内容不同抛出 `MessageIdConflict`。发布被拒绝不会启动新运行。

具有协议生命周期的 codec 可以实现 `MessagePublicationPolicy`：`validate_publication()` 校验外部消息，`starts_publication()` 与 `ends_publication()` 识别目标运行边界。判定结果与源消息原子提交，存储后端无需识别具体协议。

## 已提交数据的三种读取方式

```python
latest = await channel.latest_seq(identity=identity)

page = await channel.read(
    identity=identity,
    after=100,
    limit=200,
)

subscription = await channel.follow(
    identity=identity,
    after=100,
)

status = await channel.get_run_status(identity=identity)
```

| API | 范围 | 结果 |
| --- | --- | --- |
| `latest_seq()` | 整个 thread | 当前最大 seq，空日志为 0 |
| `read()` | 整个 thread | 一页 `DecodedMessage`，不会等待新消息 |
| `follow()` | 指定 run | 先回放，再等待该 run 的权威终止 |
| `get_run_status()` | 指定 run | 不取得 owner 的当前 durable 状态 |

线程级方法按完整 `(namespace, thread_id)` 定位日志，参数使用 `RunIdentity`；运行级方法再按 `run_id` 定位。

`get_run_status()` 可原子把过期 producer lease 归档为 `owner_lost`。没有 durable
run 时抛出 `RunNotFound`，且不会创建或恢复 producer。

## Envelope

每条提交结果是 `MessageEnvelope`：

| 字段 | 作用 |
| --- | --- |
| `channel` | codec 命名空间 |
| `identity` | 完整 namespace、thread_id 和 run_id |
| `seq` | thread 内连续位置 |
| `message_id` | thread 内稳定的消息幂等 ID |
| `codec` | 持久化格式 ID |
| `payload` | 编码后的 bytes |
| `created_at` | 首次提交时分配的 UTC 时间 |


## 提交观察函数

如果需要在 owner 新提交后更新投影：

```python
async def on_committed(envelope) -> None:
    await projection_queue.put(envelope)


subscription = await channel.wrap(
    source,
    identity=identity,
    on_committed=on_committed,
)
```

观察函数只对 owner 的新提交调用，附着回放不会重复调用。观察失败会记录日志，但不会改变已经提交的 run 结果。

## 删除 thread 日志

```python
await channel.delete_stream(identity=identity)
```

删除范围是完整 `(namespace, thread_id)`。有效的生产者租约会触发 `StreamDeleteConflict`，租约已失效则允许删除；线程缺失或已删除时重复调用仍成功。再次使用相同线程会创建新代际，旧句柄保留原删除错误。

下一篇：[取消、延迟创建与恢复](cancellation-and-recovery.md)。
