# 持久化状态与扩展

[文件与命令](rooted-filesystem.md) · [English](../../en/sandbox/persistence-and-extensions.md)

持久化 State 让多个进程共享物理 Sandbox 绑定，并在重启后恢复绑定及其用途、租约、可用性和待清理资源。
项目文件、HOME、缓存和依赖环境仍位于 Sandbox 内，需要另行备份。配置 `OpenSandboxConfig(ttl=None)`
后，新建的持久 Sandbox 保留至显式销毁；关闭管理器会保留绑定。

## SQL 数据库

```bash
pip install "tinkerfin-sandbox[sqlalchemy]" aiosqlite
```

PostgreSQL 安装 `asyncpg`，MySQL 安装 `asyncmy`。将应用管理的 Engine 交给 State：

```python
from sqlalchemy.ext.asyncio import create_async_engine

from tinkerfin_sandbox import OpenSandboxManager, SQLAlchemyOpenSandboxState

engine = create_async_engine("sqlite+aiosqlite:////var/lib/app/sandboxes.db")
state = SQLAlchemyOpenSandboxState(engine=engine, namespace="production")
manager = OpenSandboxManager(client=client, state=state)

try:
    async with manager:
        project = manager.workspace("users/7", workspace_key="project-a")
        async with project.open() as files:
            await files.awrite("/notes.txt", "hello")
finally:
    await engine.dispose()
```

PostgreSQL 使用 `postgresql+asyncpg://...`，MySQL 使用 `mysql+asyncmy://...`，三种数据库共用同一套 State API。
不支持 MariaDB。

| 参数 | 默认值 | 用途 |
| --- | --- | --- |
| `engine` | 必填 | 借用的 SQLAlchemy 异步 Engine |
| `namespace` | `""` | 多个管理器共同使用的部署域 |
| `lease_ttl` | `15.0` | 工作进程与资源领取租约的秒数 |
| `poll_interval` | `0.05` | 领取轮询和首次锁重试的间隔秒数 |
| `sqlite_retry_timeout` | `5.0` | SQLite 锁重试的最长秒数 |

同一部署域中的进程必须使用一致的预热容量。这个部署域与选择物理 Sandbox 的所有者 key、选择项目的
`workspace_key` 各自独立。Runtime namespace 限定逻辑持久化范围，不改变这两个选择，详见
[使用指南](index.md)。

释放借用的 Engine 前先关闭 State。连接和语句超时由 Engine 配置；已接受的操作和必要清理可能延长等待。

SQLite 内存数据库使用 `AsyncAdaptedQueuePool`，配置 `pool_size=1, max_overflow=0`；
不接受 `StaticPool`。`sqlite_retry_timeout` 限制 SQLite 锁重试的等待时间。

## 生成数据库结构

```python
from pathlib import Path
from tinkerfin_sandbox import get_sqlalchemy_opensandbox_state_schema

schema = get_sqlalchemy_opensandbox_state_schema(dialect="postgresql")
Path("opensandbox-schema.sql").write_text(schema.ddl, encoding="utf-8")
```

`dialect` 接受 `postgresql`、`mysql` 或 `sqlite`，返回值同时提供 `table_names`。
启动会创建空数据库中的表，或检查已有表、列、主键、索引和数据库注释。建表需要 DDL 权限；提前建立完整结构后，可使用 DML 账号。

## 预热 Sandbox

```python
config = OpenSandboxConfig(warm_pool_size=2)
manager = OpenSandboxManager(
    client=client,
    key_resolver=key_resolver,
    state=state,
    warm_pool_size=2,
)
```

`get()` 将预热实例分配给原始命令 Sandbox。隔离工作区绕过预热池，单独创建具有所需隔离能力的实例。
只使用隔离工作区的应用可设置 `warm_pool_size=0`。

## 准备工作区

```python
project = manager.workspace("users/7", workspace_key="project-a")
async with project.open() as files:
    await files.aupload_files([("/input/data.csv", csv_bytes)])
    result = await files.aexecute("mkdir -p output")
```

运行智能体前，通过 `project.open()` 预置文件或安装项目依赖。上下文关闭进程和网络后，项目数据仍会保留。

Client 的 `initializers` 用于整个物理 Sandbox 的可信初始化，在创建及每次连接后执行，必须幂等并保留
已有项目。I/O 使用异步回调并传播取消；同步回调必须非阻塞。连接和初始化共用 Client 与恢复策略中较早的
截止时间。初始化失败会抛出 `OpenSandboxInitializationError`，不会触发重试或重建。回调和时限约束见
[使用参考](api-reference.md)。

## 如果已有自己的状态存储

可以实现 `OpenSandboxState`，把绑定和租约保存到现有数据库。需要同时实现以下几组能力：

| 能力 | 方法 |
| --- | --- |
| 生命周期 | `start()`、`aclose()` |
| owner | `acquire_owner()`、`renew_owner()`、`bind_owner()`、`unbind_owner()`、`release_owner()`、`read_binding()` |
| warm pool | `claim_warm_slot()`、`claim_ready_warm_slot()`、`renew_warm()`、`publish_warm()`、`discard_ready_warm_slot()`、`release_warm()`、`warm_pool_ready()`、`consume_warm()` |
| cleanup | `enqueue_cleanup()`、`claim_cleanup()`、`renew_cleanup()`、`complete_cleanup()`、`release_cleanup()` |
| 关闭恢复 | `shutdown_sandbox_ids()` |

自定义状态必须有原子 claim、代际 fencing、租约续期和幂等释放。
`bind_owner(claim, sandbox_id, purpose=...)` 必须原子提交 ID、代际及必填的 `commands` 或
`workspaces` 用途。替换实例或远端不可用时，用途保持不变；只有显式解除绑定后才能选择其他用途。
读取结果和取得的 claim 必须包含完整绑定，未绑定的所有者没有用途。预热池只能保存 `commands`
实例；`consume_warm()` 必须拒绝已绑定 `workspaces` 的所有者，并保留原绑定和预热容量。

ready slot 在远端检查期间必须保留
已发布 ID；确认 ID 不可用后，清空 slot 与加入 cleanup 队列必须是一次原子转换。网络超时后结果不确定
时，不得擅自销毁可能已经成为权威绑定的 Sandbox。

`InMemoryOpenSandboxState(namespace=...)` 可以作为行为参考，但它不适合跨进程共享。

下一篇：[Sandbox 使用参考](api-reference.md)。
