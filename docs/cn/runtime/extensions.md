# Runtime 扩展

[流与 SSE](streams-and-sse.md) · [English](../../en/runtime/extensions.md)

## 协调同范围运行

同一进程内的相同 thread 不能并行执行时，使用内存协调器：

```python
from tinkerfin import TinkerFin
from tinkerfin.coordination import InMemoryRunCoordinator

coordinator = InMemoryRunCoordinator(
    key_resolver=lambda identity: identity.thread_id,
)
runtime = (
    TinkerFin(run_coordinator=coordinator)
    .with_namespace("company-a")
    .build(model=model, tools=tools)
)
```

默认 key 包含完整的 namespaced thread 身份。应用也可通过 `key_resolver` 明确选择其他协调粒度。协调器只控制并发执行，不持久化消息或 checkpoint。

多个 Worker 需要共享协调时，安装 `tinkerfin[redis]` 并使用
`tinkerfin.redis.RedisRunCoordinator`。应用负责关闭自己传入的 Redis client。

## 持有 Redis 资源租约

`tinkerfin.redis.RedisLeaseLock` 为应用资源提供可续租锁：

```python
from tinkerfin.redis import RedisLeaseLock

lock = RedisLeaseLock.from_client(redis, key_prefix="my-app:locks")

async with lock:
    async with lock.hold("invoice-42") as lease:
        await update_invoice(fencing_token=lease.fencing_token)
```

租约丢失会取消持有它的 Task。下游存储需要拒绝旧持有者时，应使用 fencing token。

## 观察运行

构建前可以添加完整 Runtime observer 或单个终态回调：

```python
runtime = TinkerFin().with_namespace(namespace).with_observer(tracer).build(model=model)
```

```python
runtime = (
    TinkerFin()
    .with_namespace(namespace)
    .with_observer(on_terminal=record_terminal)
    .build(model=model)
)
```

Observer 必须异步执行，框架会等待其完成并向调用方暴露失败。终态回调只在当前进程执行，不提供可靠通知投递。

## 选择集成 Profile

Runtime Profile 和原生流 Driver 是高级扩展契约：

```python
from tinkerfin import TinkerFin
from tinkerfin.runtime_profile import DeepAgentsV3RuntimeProfile

builder = TinkerFin(runtime_profile=DeepAgentsV3RuntimeProfile())
```

Profile 契约由 `tinkerfin.runtime_profile` 导出，Driver 契约由
`tinkerfin.native_driver` 导出。v3 Profile 使用 LangGraph 的实验性事件流。

下一步：[Runtime API](api-reference.md)。
