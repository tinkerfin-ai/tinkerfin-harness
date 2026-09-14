# Messaging 入门

[文档首页](../index.md) · [English](../../en/messaging/index.md)

Messaging 把一次性对象流变成可以保存、回放、附着和远程取消的生产者。浏览器断开后，Agent 可以继续运行；重新连接时从最后一个 durable 序号继续读取。

## 安装

基础 Messaging 与具体协议无关：

```bash
pip install tinkerfin-messaging
```

按宿主实际使用的 codec 和 backend 安装 extra。下面的示例需要 AG-UI：

```bash
pip install "tinkerfin[agui]" "tinkerfin-messaging[agui]"
pip install "tinkerfin-messaging[native]"
pip install "tinkerfin-messaging[agui,redis]"
```

## 把 TinkerFin 流变成可续传 SSE

```python
from contextlib import aclosing

from tinkerfin import TinkerFin
from tinkerfin_messaging import Messaging

runtime = TinkerFin().with_namespace("customer-1").build(model=model, tools=tools)
source = runtime.open_agui_run(
    thread_id="thread-42",
    run_id="run-7",
    input=graph_input,
)

async with Messaging() as messaging:
    channel = messaging.channel(name="agent-events")
    body = await channel.open_sse(source, after=0)
    async with aclosing(body):
        async for chunk in body:
            await send_to_client(chunk)
```

Runtime 自动提供 namespace、运行身份和消息格式。Messaging 确定生产者后才打开 Agent 或
Sandbox，附着请求直接复用已有运行。每个 source 只能使用一次，未被选中的候选 source
也会关闭。提前退出或 HTTP 建立失败时，调用方负责关闭返回的 SSE 流。直接传入 Runtime
对象流，无需提前编码为 SSE。

只有需要更新业务投递状态时，才在 channel 上配置 `on_source_ready` 和
`on_delivery_not_started`。附着请求不会调用这两个函数。模型、工具、输入及 HTTP 发送由应用提供。

`AgUiCodec` 需要 `[agui]`，`NativeStreamPartCodec` 需要 `[native]`，`RedisBackend` 需要
`[redis]`，`SqlAlchemyBackend` 需要 `[sqlalchemy]` 和所选异步驱动。

## 自定义 source

自定义 source 没有 RunIdentity profile，需要显式提供一次：

```python
channel = messaging.channel(name="custom", codec=codec)
subscription = await channel.wrap(
    source,
    identity=identity,
    after=0,
)
```

如果 source 自带 RunIdentity，又显式提供了不同值，Messaging 会在 backend prepare 和 source 打开前拒绝。

## 核心概念

| 名称 | 作用 |
| --- | --- |
| channel name | 一种稳定的消息格式，例如 AG-UI |
| `RunIdentity.namespace` | 应用定义的数据隔离范围 |
| `RunIdentity.thread_id` | thread 级有序日志、generation 和回放游标 |
| `RunIdentity.run_id` | 一次语义生产者，也是调用方的幂等 key |
| `seq` | thread 日志内从 1 开始的连续提交位置 |

同一个 `RunIdentity` 永远表示同一次语义运行。网络重试、附着和回放复用它；新输入使用新的 `run_id`。Messaging 不比较请求正文，权限、正文一致性和业务幂等由调用方负责。

## `after` 游标

| 值 | 行为 |
| --- | --- |
| `None` | 在 prepare 时捕获当前末尾，只接收之后的数据 |
| `0` | 从第一条保留消息开始 |
| `N` | 返回 `seq > N` 的消息 |

负数或超过当前末尾会抛出 `InvalidCursor`。

## 应用生命周期

通常在应用启动时创建一个 `Messaging`，关闭时等待它拥有的生产者安全结束：

```python
async with Messaging(backend=backend) as messaging:
    channel = messaging.channel(name="agent-events")
    await serve_application(channel)
```

关闭会等待生产者及其资源清理完成。释放借用的存储资源前，应先关闭 Messaging。

## 下一步

- [投递、回放和 SSE](delivery-and-replay.md)
- [取消、延迟创建与恢复](cancellation-and-recovery.md)
- [存储、消息格式和自定义后端](backends-and-codecs.md)
- [Messaging 使用参考](api-reference.md)
