# Sandbox 生命周期

[Sandbox 入门](index.md) · [English](../../en/sandbox/lifecycle.md)

`OpenSandboxManager` 为每个业务 key 保存一个稳定 handle。远端 Sandbox 失效或被替换后，调用方仍可继续使用同一个 handle 对象。

## Manager 配置

```python
manager = OpenSandboxManager(
    client=client,
    key_resolver=lambda key: str(key),
    state=None,
    warm_pool_size=None,
    fail_on_startup_warmup_error=False,
    settlement_timeout=None,
    recovery_policy=None,
    observers=(),
    notification_options=None,
)
```

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `client` | 必填 | 创建、连接、检查和销毁 Sandbox 的异步 client |
| `key_resolver` | `None` | 自定义 key 需要解析为稳定非空字符串；字符串 key 可直接使用 |
| `state` | `None` | 绑定和租约状态；默认使用内存状态 |
| `warm_pool_size` | `None` | 覆盖配置中的预热数量 |
| `fail_on_startup_warmup_error` | `False` | 预热失败时是否让 `start()` 直接失败 |
| `settlement_timeout` | `None` | 调用方等待 manager 关闭的最长秒数 |
| `recovery_policy` | `None` | 原实例的恢复策略；默认失败后保留实例 |
| `observers` | `()` | 借用的异步生命周期观察者 |
| `notification_options` | `None` | 每个观察者的待投递容量和执行时限 |

warm-pool 数量必须是严格整数。命令与生命周期 timeout 必须是有限数值；布尔值会在 State 启动或
创建 task 前被拒绝。

## 常用操作

| 方法 | 作用 | 远端 ID 是否通常变化 |
| --- | --- | --- |
| `get(key)` | 首次创建 Sandbox，或恢复已有绑定 | 仅显式允许重建时变化 |
| `reconnect(key)` | 重连已有绑定；没有绑定时报错 | 不变化 |
| `recreate(key)` | 创建替代实例，并安全退役旧实例 | 会变化 |
| `reset(key)` | 清空 workspace root 的内容 | 不变化 |
| `pause(key, timeout=30.0)` | 排空登记持有者的工作，并确认远端暂停 | 不变化 |
| `resume(key, timeout=30.0)` | 恢复原实例并返回可用 backend | 不变化 |
| `destroy(key)` | 销毁已知实例并移除绑定 | 被删除 |
| `delete(key)` | `destroy()` 的同义入口 | 被删除 |
| `is_healthy(key)` | 检查当前实例是否健康 | 不变化 |
| `get_details(key)` | 返回运行状态和 owner 信息 | 不变化 |
| `get_diagnostic_logs(key, scope="container")` | 通过控制面读取指定范围的供应商日志 | 不变化 |
| `get_diagnostic_events(key, scope="runtime")` | 读取指定范围的供应商事件诊断 | 不变化 |
| `check_ready()` | 预热容量未通过真实验证时抛出异常 | 不变化 |

```python
backend = await manager.get(project_key)

details = await manager.get_details(project_key)
```

大多数请求只需要 `get()`。不要在每次请求前主动 `recreate()`，否则会失去复用和预热的意义。

## 暂停与恢复

```python
backend = await manager.get(project_key)
await manager.pause(project_key, timeout=30.0)
backend = await manager.resume(project_key, timeout=30.0)
```

`pause()` 保留远端 ID、文件和已有 handle，并等待共享同一 State 的各 Manager 中的工作结束。
失联的工作进程或结果未确认的远端操作可能使暂停无法完成。

`timeout` 默认为 30 秒，必须为正有限数值；必要清理可能延长总等待时间。
远端结果未确认时保持关闭访问，不能根据超时判断实例已暂停。

通过 Manager 暂停的实例必须显式调用 `resume()`；`get()`、`reconnect()` 和 `reset()` 不会唤醒它。
恢复返回原实例的可用 backend，并重新执行连接初始化函数，因此初始化必须幂等。
连接刷新失败的 handle 暂时不可用。

官方 OpenSandbox Server 0.2.3 使用 Docker pause/unpause。`resume()` 不能启动通过 Docker 停止的
容器，也不能恢复已到期的实例。暂停不会冻结或延长远端 TTL；采用有限生存时间的暂停实例仍可能到期。

## 诊断查询

```python
logs = await manager.get_diagnostic_logs(project_key, scope="container")
events = await manager.get_diagnostic_events(project_key, scope="runtime")
```

两种查询都使用已有绑定和控制面，不创建、连接、初始化、续期或唤醒 Sandbox。没有绑定时抛出
backend 错误。Docker 日志范围为 `container` 和 `all`，事件范围为 `runtime` 和 `all`。
Docker 事件诊断描述当前运行状态，不构成完整历史事件流。

不可变的 `OpenSandboxDiagnosticContent` 区分内联 `content` 和带到期时间的 `content_url`。
`content_type`、可选的字节长度、`truncated` 和 `warnings` 说明返回内容及来源限制。
框架不会自动下载 URL。宿主负责诊断正文、引用的访问权限及运维数据留存。

## 远端实例生存时间

`OpenSandboxConfig.ttl` 默认为从创建或续期起计算的 2 小时。需要自动到期时传入大于 0 的
`timedelta`；工作区应保留到明确清理时，传入 `None`：

```python
config = OpenSandboxConfig(ttl=None)
```

使用 `ttl=None` 创建的实例没有预定到期时间。重连不会移除已有实例的到期时间。未设置到期时间的实例会持续
占用资源；不再需要时，应调用 `destroy(key)`。

绑定需要在 Manager 正常关闭后继续使用时，应选择持久 State。默认内存 State 仍在关闭时
销毁所属实例。手动清理不会持久化或备份文件，外部删除和存储故障仍可能导致文件丢失。
文件必须跨实例故障保留时，应配置持久卷和备份策略。

## 启动和关闭

`async with manager` 会自动调用 `start()` 和 `aclose()`。需要手工控制时：

```python
await manager.start()
try:
    backend = await manager.get(key)
finally:
    await manager.aclose()
```

`start()` 可以重复调用；manager 关闭后不能重新启动。

宿主就绪检查应调用 `await manager.check_ready()`；配置的预热容量不可用时会抛出
`OpenSandboxWarmPoolUnavailableError`。需要预热失败即阻止启动时，设置
`fail_on_startup_warmup_error=True`。后台预热容量故障不会使请求正在使用的实例失效。

关闭会等待正在进行的创建、替换、重置、暂停/恢复协调和清理安全结束。有限的 `settlement_timeout` 只限制当前调用方等待，不会取消 manager 已经接管的清理任务。超时会抛出 `OpenSandboxSettlementTimeoutError`，稍后可以再次调用 `aclose()` 继续等待。

默认内存 State 的远端实例归当前 Manager 生命周期所有，关闭时会销毁。持久 State 则保留绑定，
供其他 worker 接续使用。恢复失败后保留实例不会改变上述关闭语义，也不会延长实例的 TTL。

## 健康检查与替换

`OpenSandboxConfig.health_command` 用于检查数据面是否可用，默认是 `printf ok`。
`get()` 默认使用 30 秒工作预算恢复同一实例，最多 3 次，包含首次检查。重试间隔从 0.5 秒开始翻倍，
上限为 2 秒。重试耗尽后抛出 `OpenSandboxBackendUnavailableError`，保留原实例和绑定。
已确认实例不存在时会跳过无效重试。

对于允许丢弃文件的工作区，可以显式选择自动重建：

```python
from tinkerfin_sandbox import OpenSandboxManager, OpenSandboxRecoveryPolicy

manager = OpenSandboxManager(
    client=client,
    key_resolver=resolve_owner,
    recovery_policy=OpenSandboxRecoveryPolicy(on_failure="recreate"),
)
```

策略还可设置 `max_attempts`、`initial_delay`、`max_delay` 和 `timeout`。只有可识别的连接或健康检查
失败才适用恢复策略；认证、权限、协议、初始化和 State 错误会直接报告，不会重试或重建。
总预算耗尽时，如果连接或初始化结果仍不明确，也会保留绑定。`reconnect()` 和 `reset()` 始终保留
远端身份，不受自动重建选项影响。普通命令、文件写入和重置操作都不会被自动重放。

工作预算覆盖连接、健康检查和退避等待。原生连接与初始化使用 Client 和恢复策略中较早的截止时间。
必要的取消和资源结算完成后才释放所有者租约，因此调用耗时可能超过工作预算。下一次恢复不会与
上一次仍在结算的初始化函数并发执行。阻塞或吞掉取消的回调无法被强制停止。

重建会先提交新实例，再退役当前绑定的旧实例，不复制文件。持久 State 保存绑定，不保存容器内容。
保留绑定无法恢复已因外部删除或 TTL 到期而丢失的文件；需要文件跨实例存续时，应配置持久卷或其他存储策略。

正在执行的操作会继续使用它开始时取得的 backend。替换完成前，旧 backend 不会被提前关闭；替换调用会等旧实例安全退役后才返回。

## 生命周期通知

宿主需要观察生命周期变化时再传入观察者；普通 `get()` 调用无需增加通知配置：

```python
from tinkerfin_sandbox import OpenSandboxLifecycleEvent, OpenSandboxManager


class SandboxEvents:
    async def on_sandbox_event(self, event: OpenSandboxLifecycleEvent) -> None:
        await record_status(event.owner_key, event.type, event.reason)


manager = OpenSandboxManager(
    client=client,
    key_resolver=resolve_owner,
    observers=[SandboxEvents()],
)
```

| 事件 | 已确认的事实 |
| --- | --- |
| `unavailable` | 现有访问或检查发现所有者的 Sandbox 不可用 |
| `recovering` | 正在重连已知故障实例，或开始创建替代实例 |
| `recovered` | 原实例重新可用 |
| `replaced` | 已绑定不同实例，并已发布通过验证的 handle |
| `recovery_failed` | 恢复或显式替换失败；原操作仍会报告异常 |
| `workspace_reset` | 显式重置已完成，配置的工作区内容已清空 |
| `destroyed` | 显式销毁和绑定移除均已完成 |
| `paused` | 已确认绑定实例的显式暂停 |
| `resumed` | 显式恢复已为原实例返回可用的本地 handle |
| `warm_capacity_degraded` / `warm_capacity_restored` | 已验证的未绑定容量变为不可用或重新可用 |

事件包含唯一 `event_id`、`type`、宿主解析的 `owner_key`、UTC 时间 `occurred_at` 和
`OpenSandboxLifecycleReason` 原因枚举。派生属性 `recovered`、`replaced` 分别说明可用性和远端身份，
字段均不可变。远端 ID 只存在于
`diagnostic_context`，仅供可信观察者使用，不得转发给浏览器。事件不包含供应商原始异常、命令、
凭据或文件内容；宿主应选择适合通知接收者的所有者标识。

`workspace_may_have_changed` 记录生命周期中可能涉及工作区变化的证据，包括确认远端已不存在、
连接初始化及其部分副作用、替换、显式重置或销毁。即使远端 ID 保持不变，该标记也会随恢复成功或
失败事件继续传递。`False` 仅表示本事件未提供这类证据，不代表文件完整、没有独立写入或初始化
副作用已回滚。新增的远端不存在或工作区副作用证据可以更新一次尚未解决的故障通知。

同一 Manager 会对一次未解决故障中的重复失败去重。成功检查仅在恢复已观察到的故障时产生事件。
临时检查可以发现故障，但在可用的受管 handle 发布前不会报告恢复成功。
首建不会报告替换；绑定提交被取消或结果不确定时，不会在通过验证的 handle 发布前报告成功。
重复显式销毁只在首次确认移除后产生一次事件。常规关闭不会发布用户故障或销毁事件。
预热容量事件的 `owner_key=None`，不会被报告成用户 Sandbox 故障。

外部变化通过 `get()`、`is_healthy()`、`get_details()` 或预热维护发现。
通知属于当前 Manager，尽力投递，不额外轮询远端用户 Sandbox，也不保证持久化或跨进程交付。
需要保存通知时，由消费方负责存储。

每个观察者使用独立顺序队列，默认最多 128 条待投递事件，另有一条正在执行，单次回调时限为 1 秒。
通过 `notification_options=OpenSandboxNotificationOptions(max_pending_events=128, timeout=1.0)`
配置。队列满时丢弃新事件；回调异常、超时和取消不会影响 Sandbox 操作或其他观察者。关闭会在资源
结算后，并发排空各观察者已接收的通知；遵守协作式取消的回调最多再等待
`(max_pending_events + 1) * timeout`。Manager 不关闭借用的观察者。

观察者必须使用非阻塞异步操作并传播取消。回调及其创建的任务不得调用当前 Manager 的资源操作、
readiness/start 或关闭方法，否则会收到 `OpenSandboxObserverReentryError`。需要操作 Manager 的
后续工作可交给宿主独立拥有的工作任务。超时无法强制停止阻塞代码或吞掉取消的回调。

## 取消安全

创建、健康检查、替换、重置、销毁和关闭开始后，即使发起它的请求被取消，manager 仍会完成必要的资源回收。调用方收到取消不代表远端清理已经结束。

对同一 Sandbox 并发调用 `OpenSandboxClient.destroy()` 会共享结果。调用方取消会等待销毁与
本地清理完成后再传播；远端已销毁时，SDK 关闭失败不会改变远端销毁结果。
Client 关闭会先等待已接受的操作，再关闭自己创建的 transport。并发关闭调用共享结果；
取消等待者不会中止清理，关闭失败可以重试。调用方传入的 transport 仍由调用方负责关闭。

## 查看详情

```python
details = await manager.get_details(key)
if details is not None:
    print(details.sandbox_id)
    print(details.available, details.healthy)
    print(details.owner_key, details.cached)
    print(details.access_state)
```

`None` 表示这个 key 没有已知绑定。`available=False` 时查看 `unavailable_reason`，它可能是 `not_found` 或 `unreachable`。
详情可用且 `expires_at=None` 时，表示实例采用手动清理生命周期；详情不可用时，到期时间为空仅表示未知。

`access_state` 独立于供应商的 `status.state`，表示框架的共享协调阶段。远端实例仍报告 `Running` 时，
框架可能正在 `draining`、`pausing` 或等待结果确认。`None` 表示未提供协调状态快照。暂停相关阶段的
详情查询只使用控制面，此时 `healthy=False` 表示没有执行数据面探测。本地已有 handle 也可能因连接
尚在刷新而暂时不接收工作。

下一篇：[受限根目录与文件操作](rooted-filesystem.md)。
