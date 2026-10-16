# 通知监听者读取资源变化

[文档首页](../index.md) · [English](../../en/notifications/index.md)

`tinkerfin-notifications` 向独立的异步监听者广播小型资源变化提示。发布者和监听者只依赖该包的契约，无需相互引用。该包不依赖 Runtime、数据库或 HTTP。

## 发布与订阅

```bash
pip install tinkerfin-notifications
```

```python
from tinkerfin_notifications import Notification, Notifications, NotificationScope


async def example():
    async with Notifications() as notifications:
        scope = NotificationScope("project", owner_id="account-42")
        async with notifications.subscribe(scope=scope) as changes:
            # 资源提交成功后再发布提示
            await notifications.publish(
                Notification(scope=scope, topic="documents.changed", key="report")
            )
            change = await anext(changes)
            return change
```

先进入订阅，再读取初始资源快照。收到匹配的提示后重新查询；查询期间再次收到提示时，记录待刷新状态，在当前查询完成后再读取一次。

`await publish(...)` 复制提示并将其放入有界发送队列，返回不代表传输已经完成。同一作用域、主题和资源键的待发送提示可以合并。队列满时，新资源键会触发 `NotificationCapacityExceeded`；该错误不应撤销已经成功的资源提交。

各订阅相互独立，缓存有明确上限。溢出或连接中断会产生 `ResyncRequired`，监听者应重新读取权威快照。资源提交与通知发布是两个操作，Redis Pub/Sub 也无法重放断连期间的提示，因此需要持续确认的状态仍应周期读取权威数据；其他资源可按业务的新鲜度要求，在恢复可见或用户刷新时重新读取。

## 选择作用域与内容

| 输入 | 含义 |
| --- | --- |
| `NotificationScope(namespace)` | 订阅该命名空间下所有拥有者的变化 |
| `NotificationScope(namespace, owner_id)` | 只订阅这个拥有者；不接收未指定拥有者的提示 |
| `subscribe(scope=None)` | 供受信任的进程内组件观察所有命名空间 |
| `topics={...}` | 筛选发布方定义的主题 |
| `key=...` | 筛选某个资源 |

作用域负责筛选，宿主负责授权。`details` 只放标识与少量定位信息；正文、凭据和签名 URL 保留在权威服务中。每个监听者获得独立解码的值。

`NotificationLimits` 限制并发订阅数、每个订阅的待处理资源数、编码大小和待发送资源数，默认分别为 1024、128、4096 字节和 128。

## 在多个应用进程之间广播

```bash
pip install "tinkerfin-notifications[redis]"
```

```python
from redis.asyncio import Redis
from tinkerfin_notifications import Notifications
from tinkerfin_notifications.redis import RedisBackend


async def serve(redis_url, serve_application):
    async with Redis.from_url(redis_url) as redis_client:
        async with Notifications(
            backend=RedisBackend(redis_client, channel="application:changes")
        ) as notifications:
            await serve_application(notifications)
```

各进程使用同一 Redis 频道，分别管理自己的 Notifications 生命周期；后端借用 Redis 客户端。先启动通知服务，再启动发布者；关闭时按相反顺序处理。通知服务会丢弃尚未发送的提示，等待已开始的发送结算，再释放自有资源；它不会关闭借用的 Redis 客户端。Redis 故障通过重新同步提示或服务错误公开，不会转为进程内内存投递。

## 使用框架发布者

把同一个已启动的 `notifications` 传给 `InMemoryTraceStore`、`SqlAlchemyTraceStore`、`MemoryAutomationStore` 或 `SqlAlchemyAutomationStore`，即可在记录变化后通知订阅者。

| 主题 | 作用域 | 资源键 |
| --- | --- | --- |
| `trace.changed` | Runtime 命名空间 | 会话 ID；定位信息包含 generation，以及可用时的运行 ID 和序号 |
| `automation.task.changed` | 调度命名空间与拥有者 | 任务 ID |
| `automation.execution.changed` | 调度命名空间与拥有者 | 执行 ID；定位信息包含任务和 Runtime 身份 |

单纯续租不发布资源变化。调度命名空间与 Runtime 命名空间可能不同，授权时应使用对应的拥有者绑定。

需要向浏览器提供经授权的 SSE 通知时，使用 [Gateway](../gateway/index.md)。
