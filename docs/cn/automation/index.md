# 运行与调度智能体任务

[文档首页](../index.md) · [English](../../en/automation/index.md)

TinkerFin Automation 可以立即执行宿主注册的 target，也可以保存一次性、固定速率或 Cron 任务后按计划执行。所有执行都受有界并发控制。默认 Store 与 Scheduler 都在当前进程内；TinkerFin Agent 执行和任务管理工具已经包含，SQLite、MySQL 和 PostgreSQL 持久化按需安装。

## 立即执行或保存任务

```python
from datetime import UTC, datetime, timedelta

from tinkerfin_automation import (
    AutomationEngine,
    AutomationService,
    FunctionTarget,
    IntervalSchedule,
)


async def summarize(request):
    return {"project_id": request.execution.input["project_id"]}


async with AutomationService(namespace="my-application") as automation:
    async with AutomationEngine(
        automation,
        targets={"project_summary": FunctionTarget(summarize)},
    ) as worker:
        execution = await automation.execute_once(
            owner_id=authenticated_user_id,
            target="project_summary",
            input={"project_id": "project-42"},
            request_id="summarize-project-once",
        )
        await worker.wait_until_idle()
        result = await automation.get_execution(
            owner_id=authenticated_user_id,
            execution_id=execution.execution_id,
        )
```

`authenticated_user_id` 由宿主认证结果提供，target 由宿主授权。JSON 输入会被复制和保存，数值必须有限，请勿包含凭据或秘密信息；Automation 不使用输入字符串动态加载代码或认证用户。任务定义使用 `get_task`、`list_tasks`、`update_task`、`pause_task`、`enable_task` 与 `delete_task`；执行历史和控制使用 `execute_once`、`run_task_now`、`get_execution`、`list_executions`、`cancel_execution`、`retry_execution` 与 `resolve_execution`。

三种单次执行方式的持久化语义不同：

| 操作 | 行为 |
| --- | --- |
| `execute_once(...)` | 立即执行但不创建任务，执行记录的 `task_id=None` |
| `run_task_now(task_id=...)` | 为已有任务追加一次立即执行，不改变原计划 |
| `create_task(schedule=OnceSchedule(...))` | 保存一个在未来明确时刻执行一次的任务 |

## 选择调度方式

在应用中只创建一次 Service 和 Engine，并让它们的上下文覆盖应用生命周期。
以下命令使用仍在运行的 Service：

```python
task = await automation.create_task(
    owner_id=authenticated_user_id,
    name="项目摘要",
    schedule=IntervalSchedule(
        every_seconds=3600,
        start_at=datetime.now(UTC) + timedelta(hours=1),
    ),
    target="project_summary",
    input={"project_id": "project-42"},
    request_id="create-project-summary",
)
```

- `OnceSchedule(at=...)` 使用一个 aware 时间
- `IntervalSchedule(every_seconds=..., start_at=...)` 使用固定速率 UTC 基准
- `CronSchedule(expression=..., timezone=...)` 使用五字段表达式和 IANA 时区

Cron 星期使用 `mon` 至 `sun`。不存在的本地时间会跳过；重复的本地时间只执行较早的 UTC 候选。`MisfirePolicy` 支持 `skip`、`latest` 和有界 `catch_up`。

`ExecutionLimits` 的排队与并发限制对已保存任务按任务生效；无任务的 `execute_once()` 执行按 owner 共享限制。所有执行还会占用 Engine 的全局并发容量。

namespace、owner、任务名称、target 和 request ID 必须是非空、无首尾空白、可编码为 UTF-8 且不含 NUL 字节的字符串。这些标识分别限制为 128、191、255、191 和 128 个 Unicode 字符。

`AutomationEngine.close()` 停止接收新工作，在 `drain_timeout` 内等待执行完成，再取消并收齐剩余任务。并发关闭共享清理；调用方取消等待，也会在清理完成后才收到取消异常。存储或续租失败会停止监督并由 Engine 报错，外部执行结果未确认时保留并发额度。

所有日程都可设置带时区的 `active_from`（包含）和 `active_until`（不包含）。
定时触发、预览和错过时间后的补跑均受有效期约束，`run_task_now()` 手动运行不受限制。
清空有效期时，传入对应边界未设置的完整日程。

## 持久化

只安装实际使用的数据库驱动：

```bash
pip install "tinkerfin-automation[sqlalchemy]" aiosqlite
```

`SqlAlchemyAutomationStore(engine)` 使用宿主提供的异步 Engine，支持 SQLite、MySQL 和 PostgreSQL。SQL extra 不包含驱动；按需安装 aiosqlite、asyncmy 或 asyncpg。关闭 Store 会等待已接受的操作完成，Engine 由宿主关闭。首次 Service 操作或 `AutomationEngine.start()` 会在执行工作或启动 Scheduler 前自动准备 Store。只有部署就绪检查或直接使用 Store 时，才需要显式调用 `await store.setup()`。

空库自动初始化需要 DDL 权限；预创建数据库必须符合当前结构，部分或不兼容结构会被拒绝。
多个 worker 使用同一 SQL Store 时，共享任务领取和并发额度。

```python
from sqlalchemy.ext.asyncio import create_async_engine
from tinkerfin_automation import SqlAlchemyAutomationStore

database = create_async_engine("sqlite+aiosqlite:///automation.db")
store = SqlAlchemyAutomationStore(database)
try:
    async with AutomationService(namespace="my-application", store=store) as automation:
        async with AutomationEngine(automation, targets=targets) as worker:
            execution = await automation.execute_once(
                owner_id=authenticated_user_id,
                target="project_summary",
                input={"project_id": "project-42"},
                request_id="summary-request",
            )
            await worker.wait_until_idle()
finally:
    try:
        await store.close()
    finally:
        await database.dispose()
```

默认 Store 由 Service 拥有；显式传入的 Store 和数据库 Engine 仍由调用方拥有。
传入 Scheduler 则将其唤醒生命周期交给 Service 和 Engine 管理。关闭顺序是 Engine、
Service、外部 Store、数据库 Engine。并发关闭共享清理；关闭等待者被取消时，先等待
清理结束，再传播取消。关闭失败在后续 close 调用中仍可观察。
不要在正在执行的调度回调内等待所属 Scheduler 或 Service 关闭，这会抛出
`AutomationLifecycleError`。回调已经结束后，其派生子任务可以正常关闭，但宿主仍须
拥有并等待该子任务。

共享 namespace 的 worker 必须注册它们可能领取到的目标。领取不按 target 名称过滤；
worker 未注册某个目标时，对应执行会失败。

## 任务命令与执行结果

下列 Service 操作均为异步方法。任务和执行命令都要传入可信的 `owner_id`。
`list_tasks` 和 `list_executions` 返回 `items` 与 `next_cursor`；`limit` 默认为50，范围为1～100。

| 使用目的 | API 与行为 |
| --- | --- |
| 读取任务 | `get_task`、`list_tasks` |
| 修改任务 | `update_task`、`pause_task`、`enable_task`、`delete_task` 要求 `expected_revision` |
| 执行已有任务 | `run_task_now` 可选传入 `expected_revision`，在原子入队时检查 |
| 查询执行 | `get_execution`、`list_executions(task_id=...)` |
| 停止执行 | `cancel_execution`；无法确认外部状态时仍需明确结算 |
| 再次尝试 | `retry_execution` 为 `failed`、`timed_out` 或 `cancelled` 创建新尝试 |
| 处理不确定状态 | 对 `needs_attention` 调用 `resolve_execution`，必须提供结果、理由和请求 ID |

暂停只影响未来唤醒；启用从当前时间计算，不补跑暂停期间的机会。删除会取消排队中的执行，
但保留历史。更新字段为 `None` 时保留原值；清空输入请传 `input={}`。

`request_id` 在 namespace 和 owner 范围内标识同一命令及输入。请求重试应复用该值；
不同命令或输入会产生 `RequestConflictError`。已提交的立即执行命令即使遇到任务修订变化，
也返回原幂等结果。业务上的再次尝试通过 `retry_execution` 创建新的执行身份，
`retry_of` 指向原尝试。

### 筛选任务和运行历史

`list_tasks(filters=TaskFilter(...))` 支持名称和状态筛选。
`list_executions(filters=ExecutionFilter(...))` 还支持 `queued_from`（包含）和
`queued_until`（不包含）的入队时间范围；时间必须带时区。`name_contains` 使用 Unicode
casefold 后的字面子串匹配，`%` 和 `_` 没有通配含义；空 `statuses` 选择所有状态。
执行名称在入队时保存，不随任务改名或删除变化；无任务或无法确定的历史名称可为 `None`。

列表返回 `items` 和 `next_cursor`，按创建时间和 ID 倒序排列。翻页时原样传递游标，
保持相同的用户、筛选条件及执行查询的任务 ID；改变条件后从无游标开始。
上一页末行被删除不影响继续翻页。查询反映当前数据，不提供冻结快照。
`summarize_tasks()` 和 `summarize_executions()` 接受相同筛选条件，按状态统计全部匹配记录，
不受当前页大小限制。

[完整有效期示例](../../../packages/tinkerfin-automation/examples/active_period.py)
演示本地任务、手动执行、筛选和统计，不需要模型密钥或网络服务。
宿主就绪检查可调用 `await worker.check_ready()`，检查工作器是否健康，不触发执行。

## 执行 TinkerFin Agent

```python
from tinkerfin import TinkerFin
from tinkerfin_automation import TinkerFinTarget

runtime = TinkerFin().with_namespace("my-application").build(model=model, tools=tools)
targets = {"project_summary": TinkerFinTarget(runtime)}
```

把这个映射传给前文的 Engine。`TinkerFinTarget` 接收 `AgentRuntime[None]`，可选
`mode="default"` 或 `mode="plan"`，并使用各次执行已经分配的 thread 和 run ID。
任务输入不能更换模型、工具或 namespace。成功时仅记录完成状态，`result=None`；
Agent 消息和输出从 Runtime 配置的 Trace 或业务存储读取。

通过仍在运行的 Service 向这个目标提交 Graph 输入：

```python
execution = await automation.execute_once(
    owner_id=authenticated_user_id,
    target="project_summary",
    input={"messages": [{"role": "user", "content": "汇总项目内容"}]},
    request_id="agent-summary-request",
)
```

普通 `FunctionTarget` 的异步函数返回有限 JSON 时，该值成为执行结果。其默认
`cancellation_is_final=False`，避免把协程取消误当成外部工作已停止。只有目标确实能
保证外部工作已经停止时，才能把该值设为 true。

## Graph interrupt

TinkerFin 结果的 `__interrupt__` 集合非空时，执行进入 `interrupted`。Automation 不批准、不拒绝，也不恢复 graph。可选 `on_interrupt` 回调返回 `None` 表示在原执行期限内保持未完成，返回 `ExecutionFailure` 表示立即失败；回调异常记为失败，回调超时使用原执行期限。

中断期间仍占用所属任务或无任务 owner 的并发额度。如果取消或超时不能证明外部工作已经停止，执行进入 `needs_attention` 并保留保护性容量。只有经过独立授权和审计的 `resolve_execution` 才能结算这种不确定状态。

## Agent 创建任务

以已认证 owner 和允许的 target 集合调用 `create_automation_tools()`。该工具能力已包含在默认安装中，返回的工具与应用代码使用同一个 Service。后台任务默认没有管理工具；宿主显式开放时，还需限制任务数量、频率、并发和派生深度。

`execute_automation_once` 创建无任务的立即执行；`run_automation_task_now` 必须提供已有任务 ID。模型提供任务参数和 JSON 输入；owner、namespace、目标权限和执行限制仍由宿主控制。

九个工具均使用绑定的 owner。创建、更新、启用、直接执行和执行已有任务都会检查实际采用的
target 是否属于 `allowed_targets`，并发修改不能替换已授权的目标。读取、暂停、删除仍按
owner 限定范围。修改类工具要求 `request_id`；工具不开放排队/错过调度策略设置、执行取消、
重试、人工结算或 Graph 恢复命令。

## 实现扩展

专门的集成契约位于现有公共模块中：

| 模块 | 公共契约 |
| --- | --- |
| `store` | `AutomationStore`、`WorkItemClaim`、`WorkKind`、`StartAuthorization`、`ScheduledExecution`、`MaterializationResult` |
| `scheduler` | `AutomationScheduler`、`MemoryScheduler`、`TaskDue` |
| `clock` | `AutomationClock`、`SystemClock`、`ManualClock` |
| `schedules` | `materialize_schedule`、`MaterializedSchedule`、调度 JSON 转换 |
| `sql_schema` | `get_automation_store_schema`、`AutomationStoreSchema`、SQL 表定义 |

`AutomationTarget.run(ExecutionRequest)` 返回成功、中断、失败或不确定结果。请求包含已保存的
执行快照和截止时间；`cancellation_is_final` 属性表达目标实际能够提供的外部停止保证。

Scheduler 实现 `start(on_task_due)`、`schedule_task`、`remove_task` 和 `close`。
它交付到期任务 ID，是否允许生成对应发生项仍由 Store 的权威状态决定。回调必须先结束，
再关闭其所属资源。Clock 提供 `now()` 与异步 `wait_until()`；测试可以通过
`ManualClock.advance()` 明确推进时间。

Store 提供异步命令、查询和状态统计，各组操作保护不同的原子边界：

| 操作 | 必须保持的不变量 |
| --- | --- |
| 任务 CRUD | owner 隔离、修订检查、命令幂等 |
| 调度读取与 `materialize_task` | 推进已观察的唤醒时间，并在同一边界写入去重后的发生项 |
| `enqueue_execution` | 发生项/命令去重、排队容量和可选任务修订检查一起完成 |
| `claim_work`、`renew_claim` | 预占共享并发额度，并核验当前所有者及 fence |
| `authorize_start` | 在调用目标前保存唯一一次开始授权 |
| `mark_interrupted`、`finish_execution` | 保留截止时间，仅在确认终态后释放额度 |
| 取消与人工结算 | 不确定工作保留额度，直到经过授权的明确结算 |
| 初始化、时间、读取与关闭 | 当前 Schema 校验、权威时间、稳定分页和借用资源归属 |

仓库的 `test_store_contract.py`、`test_store_atomicity.py`、`test_sql_queue_admission.py`、
`test_tool_permissions.py` 覆盖这些契约；`store_with_clock` fixture 将共用场景运行在
Memory 和 SQL Store 上。验证其他实现时使用受控时钟、同步信号和隔离数据库，不能通过重放
外部目标副作用来确认数据库提交结果。
