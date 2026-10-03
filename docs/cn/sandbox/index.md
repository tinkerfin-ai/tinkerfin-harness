# Sandbox

[文档首页](../index.md) · [English](../../en/sandbox/index.md)

`tinkerfin-sandbox` 提供异步文件与命令、可复用隔离环境、持久绑定、预热容量、暂停、恢复和清理。

## 安装

```bash
pip install tinkerfin-sandbox
```

需要持久绑定时，安装 SQLAlchemy extra 和一种异步驱动：

```bash
pip install "tinkerfin-sandbox[sqlalchemy]" aiosqlite
```

PostgreSQL 使用 `asyncpg`，MySQL 使用 `asyncmy`。

隔离工作区需要部署 TinkerFin [运行镜像](https://github.com/tinkerfin-ai/sandbox-runtime)及配套的
[Server](https://github.com/tinkerfin-ai/sandbox-runtime/blob/main/opensandbox-server/README.md#deploy)
和 [execd](https://github.com/tinkerfin-ai/sandbox-runtime/blob/main/opensandbox-execd/README.md#deploy)。
应用使用 Manager API，框架负责每次运行的环境、认证和清理。

## 在 AgentRuntime 中使用 Sandbox

应用决定 Sandbox 所有者和项目：

- `key` 选择物理 Sandbox，例如 `"users/7"`
- `workspace_key` 选择其中一个项目的文件、HOME、缓存和依赖环境

Runtime `namespace` 限定逻辑持久化范围，不改变工作区使用的物理 Sandbox 或项目。

```python
from opensandbox.config import ConnectionConfig

from tinkerfin import TinkerFin
from tinkerfin_sandbox import OpenSandboxClient, OpenSandboxManager

client = OpenSandboxClient(
    connection_config=ConnectionConfig(domain="127.0.0.1:8091"),
)

async with OpenSandboxManager(client=client) as sandboxes:
    project = sandboxes.workspace("users/7", workspace_key="project-a")
    runtime = (
        TinkerFin()
        .with_namespace("projects/project-a")
        .build(
            model=model,
            backend=project,
        )
    )
    result = await runtime.ainvoke(
        thread_id=thread_id,
        run_id=run_id,
        input=graph_input,
    )
```

`workspace(...)` 返回 `SandboxWorkspace`，不执行 I/O。Runtime 只为已准入的运行准备隔离访问，
并在清理时停止本次运行的进程和网络活动。同一项目跨对话复用时保留文件；一次运行结束不会销毁 Sandbox。

## 直接管理 Sandbox

在智能体运行之外访问文件和执行命令时，使用同一个项目：

```python
project = sandboxes.workspace("users/7", workspace_key="project-a")
async with project.open() as files:
    await files.aupload_files([("/notes.txt", b"hello")])
    result = await files.aexecute("cat notes.txt")
```

项目不再需要时，调用 `await project.delete()` 停止该项目的全部运行，并删除其文件、HOME、缓存和
依赖环境。同一 Sandbox 中的其他项目仍可继续使用。

| 任务 | 方法 |
| --- | --- |
| 打开或复用原始命令 Sandbox | `get(key)` |
| 重连原始命令 Sandbox | `reconnect(key)` |
| 替换原始命令 Sandbox | `recreate(key)` |
| 清空原始命令 Sandbox 的文件根目录 | `reset(key)` |
| 暂停或恢复所有者的整个 Sandbox | `pause(key)`、`resume(key)` |
| 销毁所有者的 Sandbox 及其全部项目 | `destroy(key)` |
| 查看状态 | `get_details(key)` |
| 关闭本地资源 | `aclose()` |

原始命令方法会拒绝已用于隔离工作区的所有者。以上例子中，`await sandboxes.pause("users/7")`
会暂停该所有者的全部项目。暂停和恢复返回 `None`；`project.open()` 不会自动恢复已暂停的 Sandbox。

## 持久化与所有权

`SQLAlchemyOpenSandboxState` 通过借用的 SQLAlchemy `AsyncEngine` 支持 SQLite、MySQL 和 PostgreSQL。
应用负责创建和释放 Engine。State 保存物理 Sandbox 绑定和生命周期协调状态。项目文件随该 Sandbox
保留；State 不备份文件、不创建项目持久卷，也不提供项目存储配额。

manager 拥有自己的 OpenSandbox client 和 State；调用方传入的 HTTP transport 仍由调用方管理。manager 关闭时，持久 State 保留远程 Sandbox，内存 State 销毁自己创建的实例。

Shell 命令、文件工具和传输使用同一项目隔离。每次运行拥有独立的进程、临时文件和网络，共用的基础
工具只读。项目文件、HOME、缓存和依赖环境在运行间保留。公网 HTTP 80 和 HTTPS 443 通过受控代理
访问；运行中启动的服务器进程随该次运行结束。

## 后续阅读

- [生命周期](lifecycle.md)
- [文件与命令](rooted-filesystem.md)
- [持久 State 与扩展](persistence-and-extensions.md)
- [Sandbox API](api-reference.md)
