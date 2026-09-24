# 运行与调度智能体任务

[文档首页](../index.md) · [English](../../en/automation/index.md)

TinkerFin Automation 运行宿主注册的目标，支持立即执行、单次日程、固定间隔和 Cron。
通过绑定用户的任务与执行句柄管理工作；存储负责请求幂等、有界队列和跨工作器容量协调。
默认内存存储适合单进程，SQLite、MySQL 和 PostgreSQL 持久化按需安装。

## 立即执行或保存任务

以下片段放在宿主异步入口内；authenticated_user_id 来自认证结果，是字符串。

```python
from tinkerfin_automation import Automation, ExecutionRequest, Schedule


automation = Automation(namespace="my-application")


@automation.target("project_summary")
async def summarize(request: ExecutionRequest) -> dict[str, str]:
    return {"project_id": str(request.input["project_id"])}


async with automation.worker():
    owner = automation.for_owner(authenticated_user_id)
    run = await owner.run(
        "project_summary",
        input={"project_id": "project-42"},
        request_id="summary-once",
    )
    await run.wait(timeout=30.0)
    print(run.status, run.result)
```

`owner.run()` 只提交无任务的执行；`owner.create_task()` 保存启用的任务定义；
`task.run()` 为已有任务追加一次执行。提交返回 RunHandle，不表示任务已完成。
Owner 是身份作用域，不是认证凭据；宿主仍需授权目标并避免把秘密信息放入 JSON 输入。

## 选择调度方式

在活动 worker 上下文中创建任务：

```python
task = await owner.create_task(
    name="Project summary",
    target="project_summary",
    schedule=Schedule.cron("0 9 * * mon-fri", timezone="Asia/Shanghai"),
    input={"project_id": "project-42"},
    request_id="create-summary",
)
```

- `Schedule.once(at=...)` 使用一个带时区的时刻
- `Schedule.every(minutes=30, start_at=...)` 使用固定起点；seconds/minutes/hours/days 为非负整数，总间隔为一分钟至365天
- `Schedule.cron(expression, timezone=...)` 使用五字段表达式和明确 IANA 时区，星期使用 mon 至 sun

工厂返回三种具体日程模型，类型注解使用判别联合 `ScheduleSpec`。
所有工厂支持 `active_from`（包含）和 `active_until`（不包含）；要清除有效期，传入完整替换日程。
有效期只影响定时发生项，不限制手动运行。请求重放保留原 at/start_at，不重新使用当前时间。
Cron 跳过夏令时缺口，重叠时只取较早 UTC 时刻；MisfirePolicy 默认 latest，也支持 skip 和有界 catch_up。

ExecutionLimits 默认每任务一次并发、十次排队、三十分钟执行期限及二十四小时排队期限。
无任务执行按 owner 共享限制，worker 默认 global_concurrency=16。

## 持久化

```bash
pip install "tinkerfin-automation[sqlalchemy]" aiosqlite
```

SQL extra 支持 SQLite、MySQL 和 PostgreSQL；MySQL 使用 asyncmy，PostgreSQL 使用 asyncpg。
宿主提供异步数据库 Engine 并设置连接和语句超时。以下片段中 host_shutdown 由宿主提供：

```python
from sqlalchemy.ext.asyncio import create_async_engine
from tinkerfin_automation import Automation, SqlAlchemyAutomationStore


database = create_async_engine("sqlite+aiosqlite:///automation.db")
store = SqlAlchemyAutomationStore(database)
try:
    automation = Automation(namespace="my-application", store=store)
    automation.target("project_summary", summarize)
    async with automation.worker() as worker:
        await worker.check_ready()
        await host_shutdown.wait()
finally:
    try:
        await store.close()
    finally:
        await database.dispose()
```

进入上下文时准备 Store。worker 退出依次关闭 Engine、Service；显式 Store 和数据库仍由宿主关闭。
默认 Store 由框架拥有；传入 Scheduler 即将其生命周期交给 Automation。关闭先停止新操作，
等待已接受的操作并收齐清理，取消不会遗留任务。运行中的目标或资源回调不能关闭其所属 Automation。

实例只能进入一次；client 和 worker 不能嵌套。`async with automation` 不启动本地 Worker，
可通过共享持久 Store 向远端 Worker 提交立即执行、查询、手动运行已有任务、取消、重试和结算执行。
日程的创建、更新、暂停、启用和删除只允许在本地 worker 模式进行。共享 SQL 本身不提供远端动态日程发现。
同 namespace 的 Worker 必须能够执行其可能领取的全部目标，领取不按 target 名称路由。

Store setup 创建空库或校验完整当前结构，不自动修补部分表。空库需 DDL 权限，预建结构必须匹配。
高级直接使用 Store 的宿主可显式调用 `await store.setup()`；数据库连接池必须保证连接独占借用。

## 任务命令与执行结果

`automation.for_owner()` 只绑定一次身份；返回对象继续保有其身份和资源作用域。

| 使用目的 | API 与行为 |
| --- | --- |
| 查询任务 | `owner.task(id)`、`owner.list_tasks()` 返回 TaskHandle |
| 修改任务 | `task.update/pause/enable/delete` 默认使用所见 revision，允许显式 expected_revision |
| 执行任务 | `task.run()` 追加一次执行，不改变日程 |
| 查询执行 | `owner.get_run(execution_id)`、`owner.list_runs(task_id=...)` |
| 等待结果 | `run.wait(timeout=30.0, poll_interval=1.0)` 只观察指定执行 |
| 请求取消 | `run.cancel()`，cancel_requested 不等于 cancelled |
| 再次尝试 | `run.retry()` 对 failed/timed_out/cancelled 返回新的 RunHandle |
| 人工结算 | 独立授权及审计后的 `owner.resolve_run()`，不恢复 Graph |

Handle 属性无 I/O，snapshot/result 是隔离的值；refresh 显式读当前状态。
成功修改更新本句柄，不同句柄不联动。失败不自动刷新或重试。
暂停只影响未来唤醒，启用不补跑暂停期间；删除取消排队执行并保留历史，最后快照仍可读取。
update 的 None 表示保留，input={} 表示清空。

远端表单必须传递用户提交的原 revision，不能用刚查询到的新版替换：

```python
task = await owner.task(task_id)
await task.pause(
    expected_revision=command.expected_revision,
    request_id=command.request_id,
)
```

重放保留 request_id、原 revision、输入和日程锚点；省略 request_id 不保证去重。
不同命令共用请求键会冲突。删除跨请求重放直接调用
`owner.delete_task(task_id, expected_revision=revision, request_id=request_id)`，不先查询已删除定义。
数据库提交后响应失败不能证明未写入，也不能据此换一个请求键再次创建。

wait 使用有限正数秒，期限包含锁等待、读取和间隔；超时抛 AutomationWaitTimeout，
取消等待不会取消执行。正常终态以及 interrupted/needs_attention 都会返回，后两者仍非终态。
业务失败通过状态及 snapshot.failure_code/failure_message 读取；Store 错误直接传播。
RunHandle.id 是执行记录 ID，identity.run_id 才是 Runtime run ID；result 可能为空。

### 筛选任务和运行历史

`list_tasks(filters=TaskFilter(...))` 支持名称和状态筛选。
`list_runs(filters=ExecutionFilter(...))` 还支持 `queued_from`（包含）和
`queued_until`（不包含）的入队时间范围；时间必须带时区。`name_contains` 使用 Unicode
casefold 后的字面子串匹配，`%` 和 `_` 没有通配含义；空 `statuses` 选择所有状态。
执行名称在入队时保存，不随任务改名或删除变化；无任务或无法确定的历史名称可为 `None`。

列表返回 `items` 和 `next_cursor`，按创建时间和 ID 倒序排列。翻页时原样传递游标，
保持相同的用户、筛选条件及执行查询的任务 ID；改变条件后从无游标开始。
上一页末行被删除不影响继续翻页。查询反映当前数据，不提供冻结快照。
`summarize_tasks()` 和 `summarize_runs()` 接受相同筛选条件，按状态统计全部匹配记录，
不受当前页大小限制。

[完整有效期示例](../../../packages/tinkerfin-automation/examples/active_period.py)
演示本地任务、手动执行、筛选和统计，不需要模型密钥或网络服务。
宿主就绪检查可调用 `await worker.check_ready()`，检查工作器是否健康，不触发执行。

## 执行 TinkerFin Agent

model、tools 和认证身份由宿主提供：

```python
from tinkerfin import TinkerFin
from tinkerfin_automation import Automation, TinkerFinTarget

runtime = TinkerFin().with_namespace("my-application").build(model=model, tools=tools)
automation = Automation(namespace=runtime.namespace)
automation.target("agent", TinkerFinTarget(runtime))
async with automation.worker():
    owner = automation.for_owner(authenticated_user_id)
    run = await owner.run(
        "agent",
        input={"messages": [{"role": "user", "content": "Summarize the project"}]},
        request_id="agent-summary",
    )
    await run.wait()
```

执行身份的 namespace 必须与 Runtime 一致。新任务默认使用 Automation 的 namespace；
同一个调度器管理多个 Runtime 范围时，创建任务或提交一次性执行前通过
`automation.for_owner(owner_id, execution_namespace=runtime.namespace)` 绑定运行范围。
任务后续执行和重试沿用已经保存的范围；任务输入不能更换模型、工具或 namespace。
TinkerFinTarget 成功只记录完成状态，result=None；消息从已配置的 Trace 或业务存储读取。
普通异步 callable 的有限 JSON 返回值会成为结果；默认取消保证为 False。
只有确实能保证外部工作停止时，才显式使用 FunctionTarget(..., cancellation_is_final=True)。

## Graph interrupt

非空原生中断使执行进入 interrupted，Automation 不审批或恢复 graph。
`automation.worker(on_interrupt=...)` 的异步回调返回 None 保留原执行期限，返回 ExecutionFailure 则失败。
回调异常使执行失败，超时沿用原期限。中断仍占用容量；外部工作未确认停止时进入 needs_attention，
只有独立授权和审计后的 owner.resolve_run 才能结算。

## Agent 创建任务

```python
from tinkerfin_automation import create_automation_tools

tools = create_automation_tools(
    automation.for_owner(authenticated_user_id),
    allowed_targets={"project_summary"},
)
```

九个通用工具使用绑定的 Owner，日程写操作要求活动 worker；模型不能覆盖 owner、namespace 或目标权限。
execute_automation_once 提交无任务执行，run_automation_task_now 运行已有任务并校验所见目标和 revision。
修改类工具要求稳定 request_id；不开放执行取消、重试、人工结算或 Graph 恢复。
宿主决定哪些 Agent 获得管理工具，并限制派生任务的数量、频率和权限。

## 实现扩展

专门的集成契约位于现有公共模块中：

| 模块 | 公共契约 |
| --- | --- |
| `service` / `engine` | `AutomationService` / `AutomationEngine` |
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
