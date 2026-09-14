# 取消、延迟创建与恢复

[投递、回放和 SSE](delivery-and-replay.md) · [English](../../en/messaging/cancellation-and-recovery.md)

这一页处理三种生产环境常见情况：用户取消运行、只有真正的生产者才创建 Agent，以及进程失效后从已提交位置恢复。

## 远程取消

先在启动生产者时提供取消函数：

```python
async def cancel_agent(context):
    running_task.cancel()
    return cancellation_tail


body = await channel.open_sse(
    source,
    identity=identity,
    cancel=cancel_agent,
)
```

随后可从另一个请求中取消：

```python
cancelled = await channel.cancel(identity=identity)
```

取消函数可以不接收参数，也可以接收 `CancelContext`。它可以返回有限的终止事件，让订阅者收到明确的取消结尾。

TinkerFin 的 AG-UI 流已经提供取消能力，直接把该流交给 Messaging 时省略 `cancel=`。

Messaging 开始关闭后不再接受新的取消请求。关闭时先通知当前生产者，再等待已经接受的
取消操作、运行收尾及资源清理完成。

| 结果或错误 | 含义 |
| --- | --- |
| `True` | 已请求取消活跃生产者 |
| `False` | run 已经结束，不需要再取消 |
| `RunNotFound` | 找不到 run |
| `CancellationUnsupported` | run 没有取消函数 |
| `RunProducerFailed` | 生产者或取消过程失败 |

## 只有 owner 才创建 Agent

直接把 Runtime 创建的惰性运行流交给 Messaging：

```python
source = runtime.open_agui_run(
    thread_id="thread-42",
    run_id="run-7",
    input=graph_input,
)
```

Messaging 选定生产者后才打开 Agent。附着请求复用已有状态，不会重复准备 Agent 或 Sandbox。

宿主投递状态使用 channel callback：

```python
body = await channel.open_sse(
    source,
    on_source_ready=activate_business_run,
    on_delivery_not_started=cleanup_business_run,
)
```

Source 自有 `on_owner_preflight` 是准备 source 本身的高级 hook，不是宿主激活的另一个名称。

## 高级 deferred source

自定义协议 source 创建成本较高时，可以使用 `DeferredMessageSource`：

```python
from tinkerfin_messaging import (
    DeferredMessageSource,
    MessageSourceBinding,
    ProfiledDeferredMessageSource,
)


async def open_events():
    events = await connect_event_source()
    return MessageSourceBinding(source=events, cancel=events.cancel)


source = DeferredMessageSource(
    open_events,
    cancellable=True,
    cancel_after_first_item=True,
)
```

`connect_event_source()` 由应用实现，返回支持异步迭代、幂等 `aclose()` 和取消的事件源。
示例通过 `cancel` 明确提供取消函数。

| 参数 | 作用 |
| --- | --- |
| `opener` | 异步创建真正 source，并返回 `MessageSourceBinding` |
| `cancellable` | 声明打开后的 source 是否支持取消 |
| `cancel_after_first_item` | 是否等第一条协议事件产生后才允许取消超过它 |
| `on_owner_preflight` | owner 专用的可选 source 准备函数，在 opener 执行前完成 |

附着或纯回放请求不会调用 opener。`cancel_after_first_item=True` 适合必须先出现 `RUN_STARTED` 的协议。

Messaging 从取得所有权起持续续租，覆盖源准备、就绪回调、消息输出和结束清理。
源工厂无需管理租约。准备期间收到取消时，会中断待完成的工作并等待资源清理；
失去所有权后不会继续发布消息。

Messaging 在 durable owner 选定后等待 `on_owner_preflight`。回调失败时会释放本次 owner 并关闭
deferred wrapper，opener 不会运行。该 hook 只用于 source 自有准备；宿主激活应放在 opener
成功后的 `on_source_ready`。

`MessageSourceBinding` 包含 `source` 和可选 `cancel`。如果 source 自己声明取消函数，可以省略 binding 的 `cancel`。

自定义 opener 打开已知 AG-UI 或 Native 流，并且 name-only channel 必须在打开前识别 codec
与 RunIdentity 时，使用 `ProfiledDeferredMessageSource`：

```python
from ag_ui.core import BaseEvent


source = ProfiledDeferredMessageSource(
    open_events,
    identity=identity,
    codec_profile="agui.event",
    source_type=BaseEvent,
    replay_type=BaseEvent,
    cancellable=True,
    cancel_after_first_item=True,
)
```

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `opener` | 必填 | 异步返回 `MessageSourceBinding` |
| `identity` | 必填 | 打开前可用的完整运行身份 |
| `codec_profile` | 必填 | 与内置 codec 对应的稳定 profile ID |
| `source_type` | 必填 | opener 产生的 live 数据类型 |
| `replay_type` | 必填 | codec 解码后的数据类型 |
| `cancellable` | 必填 | source 是否支持远程取消 |
| `cancel_after_first_item` | `False` | 是否防止取消越过第一条协议事件 |
| `on_owner_preflight` | `None` | 选定新生产者后、打开事件源前执行的准备函数 |

## 固定事件与转换事件

```python
from tinkerfin_messaging import FiniteMessageSource, map_source


finite = FiniteMessageSource.from_events((started, finished))


async def enrich(event):
    return add_request_metadata(event)


mapped = map_source(source, enrich)
```

`FiniteMessageSource` 适合已知的有限事件。`map_source()` 可以使用同步或异步转换函数，并保持原 source 的顺序、背压、取消尾部和关闭行为。

转换后类型可能改变，因此 `map_source()` 的结果不会继续声明原来的内置 codec；使用它时给 channel 显式配置 codec。

关闭调用被取消后，释放借用资源前应再次等待 `aclose()` 完成。

一个订阅只允许一次正在进行的拉取。调用 `subscription.aclose()` 会先取消并结算该次拉取，再关闭
解码与后端迭代器；等待下一条消息的消费方会收到 `CancelledError`。生产者继续独立运行，后续订阅
仍可重播已提交的消息。

## 进程失效后恢复 source

如果 source 可以从稳定位置重建，实现 `RecoverableSource` 并使用 `wrap_recoverable()`：

```python
subscription = await channel.wrap_recoverable(
    recoverable_source,
    identity=identity,
    after=0,
)
```

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `source` | 必填 | 实现 `open(checkpoint)` 的可恢复 source |
| `identity` | profile source 可省略 | 自定义 source 的运行身份，或对 profile RunIdentity 的一致性检查 |
| `after` | `None` | 独占回放游标；`None` 从 prepare 时的当前末尾开始 |
| `cancel` | `None` | 接受远程取消后停止 source，并可返回带稳定 ID 的有限尾部 |
| `on_committed` | `None` | owner 每次成功提交后的异步观察函数 |

`recoverable_source.open(checkpoint)` 返回的每一项都是 `RecoverableMessage`：

```python
RecoverableMessage(
    message_id="source-event-17",
    data=event,
    checkpoint=RecoveryCheckpoint(
        position=b"17",
        last_message_id="source-event-17",
    ),
)
```

| 字段 | 作用 |
| --- | --- |
| `message_id` | 稳定、可重试的消息 ID |
| `data` | 要编码并保存的数据 |
| `checkpoint.position` | source 自己解释的恢复位置 |
| `checkpoint.last_message_id` | 必须与当前 `message_id` 相同 |

稳定 message ID 只能让消息提交幂等，不能自动保证模型调用、工具调用或数据库写入只发生一次。外部副作用仍需要业务幂等键或 outbox。

## 关闭与 settlement timeout

`Messaging(settlement_timeout=...)` 的值只限制调用方等待清理的时间，不会粗暴取消已经接管的提交、取消尾部或 source 关闭任务。超时后会抛出 `MessagingSettlementTimeout`；稍后再次调用 `aclose()` 会继续等待同一个清理任务。

下一篇：[Redis、自定义 codec 和 backend](backends-and-codecs.md)。
