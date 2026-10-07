# 受限根目录与文件操作

[Sandbox 生命周期](lifecycle.md) · [English](../../en/sandbox/rooted-filesystem.md)

隔离工作区把项目文件映射为文件工具的 `/`。所有者 key 选择物理 Sandbox，`workspace_key`
选择其中的项目：

```python
project = manager.workspace("users/7", workspace_key="project-a")
```

## 为什么使用受限根目录

- 文件工具拒绝走出项目文件范围的路径和链接；
- Shell 命令、文件传输和输出捕获使用同一项目隔离；
- 项目文件、HOME、缓存和依赖环境在运行间保留；
- Agent 使用虚拟路径，不必知道物理存储目录。

每次运行拥有独立的进程、`/tmp`、`/proc`、`/dev` 和网络，共用的基础工具只读。运行或 `open()`
上下文结束时会停止其中的进程和网络活动，包括取消和异常退出。同一项目的并发运行共享文件，写入不具备
事务保证。

通过 `manager.get()` 打开的原始命令 Sandbox 可以设置 `workspace_root="/workspace"`，把文件工具的
`/` 映射到该目录，`reset()` 则清空其子项。这个配置只限制文件工具，原始 Shell 仍能访问 Sandbox 内
其他路径；它不配置隔离工作区，隔离工作区的虚拟文件根始终为 `/`。

## 常用异步文件操作

```python
async with project.open() as backend:
    await backend.awrite("/notes.txt", "hello")
    result = await backend.aread("/notes.txt")
    await backend.aedit("/notes.txt", "hello", "hello world")
    entries = await backend.als("/")
    matches = await backend.aglob("**/*.txt", "/")
    hits = await backend.agrep("hello", "/", glob="*.txt")
    await backend.adelete("/notes.txt")
```

### 方法参数

| 方法 | 参数 | 作用 |
| --- | --- | --- |
| `aread()` | `file_path`、`offset=0`、`limit=2000` | 按行读取文本 |
| `awrite()` | `file_path`、`content` | 写入完整文本 |
| `aedit()` | `file_path`、`old_string`、`new_string`、`replace_all=False` | 精确替换文本 |
| `adelete()` | `file_path` | 删除文件或目录，但不能删除虚拟根 |
| `als()` | `path` | 列出目录 |
| `aglob()` | `pattern`、`path=None` | 查找路径 |
| `agrep()` | `pattern`、`path=None`、`glob=None`、`max_count=None` | 搜索文本 |

每个结果对象都可能包含正常数据和错误说明。批量或搜索场景不要只检查列表是否为空，也要检查结果中的 error。

## 查看已有文件

查看文件不应启动执行环境时，直接使用项目声明。Manager 必须在这些调用期间保持开启：

```python
project = manager.workspace("users/7", workspace_key="project-a")
page = await project.list_directory("/", limit=100)
while True:
    for entry in page.entries:
        print(entry.path, entry.kind, entry.size_bytes)
    if page.next_cursor is None:
        break
    page = await project.list_directory("/", limit=100, cursor=page.next_cursor)

info = await project.get_file_info("/notes.txt")
preview = await project.read_text("/notes.txt", max_bytes=100 * 1024, max_lines=200)
print(preview.text, preview.truncated)
```

这些读取不会创建、重建、初始化或恢复资源。宿主负责选择已授权的所有者与项目标识，框架负责限制
路径范围、读取大小和关闭查询资源。工作区尚未初始化时可呈现无文件状态；暂停或连接不可用仍是
明确错误。路径以虚拟 `/` 为根，不跟随符号链接。字段、限制与异常见 [API 参考](api-reference.md)。

## 订阅文件变化

项目已存在且正在运行时，先建立订阅，再读取初始文件状态。订阅不会打开命令运行，也不会阻止
Sandbox 暂停：

```python
async with project.watch() as changes:
    print(await project.list_directory("/"))
    async for change in changes:
        print(await project.list_directory("/"))
```

迭代结果为 `tinkerfin_sandbox.WorkspaceChange.FILES_CHANGED` 或
`tinkerfin_notifications.ResyncRequired`。两者都提示整个项目文件根可能发生变化，需要通过文件接口
读取当前状态；提示可以合并，不包含文件正文、逐条操作或可重放历史。先订阅再读取初始快照，收到
重新同步提示后再次读取权威状态。

订阅覆盖文件工具、上传和普通 Shell、Python 写入，以及截断、重命名和删除。不观察 HOME、缓存、
依赖目录，也不覆盖内存映射写入或被新挂载遮住的文件。目录变化后重新建立监听时可能要求重新同步。

订阅不会创建或恢复 Sandbox、项目。暂停、删除、替换实例或源端断连后，当前订阅会给出断连重新同步
提示并结束；重新订阅前需确认资源可用。退出或取消上下文只释放当前订阅，不影响项目文件和其他监听者。

## 执行命令

```python
async with project.open() as backend:
    result = await backend.aexecute(
        "python -m pytest",
        timeout=300,
    )
```

`timeout=None` 使用 backend 的默认命令超时。命令从项目文件目录开始；命令中使用相对路径，或通过
`backend.to_shell_path(file_path)` 转换虚拟文件路径。命令超时或取消会结束当前运行，已写入的项目
文件会保留。

公网 HTTP 80 和 HTTPS 443 通过受控代理访问，支持 `pip`、`npm`、HTTPS Git 和浏览器请求，应用
无需自行配置代理。私有目标地址、SSH Git 和其他目标端口不可用。一次运行启动的服务器不会持续到后续运行。

如果命令输出可能很大，创建 Manager 时为 Client 传入
`OpenSandboxConfig(enable_capture_offload=True)`，然后保存命令输出：

```python
async with project.open() as backend:
    result = await backend.aexecute_with_offload(
        "python -m pytest -vv",
        "/captures/tests.txt",
        max_inline_bytes=32_000,
        max_capture_bytes=5_000_000,
        timeout=300,
    )
```

| 参数 | 作用 |
| --- | --- |
| `capture_path` | 大输出在虚拟根中的保存位置 |
| `max_inline_bytes` | 直接放在结果中的最大 bytes |
| `max_capture_bytes` | 可选的完整捕获上限 |
| `timeout` | 命令超时秒数 |

## 上传和下载

```python
async with project.open() as backend:
    uploads = await backend.aupload_files([("/input/data.csv", csv_bytes)])
    downloads = await backend.adownload_files(["/output/report.json"])
```

输入顺序和响应顺序一致。某个明确无效的路径只影响对应项；网络失败或结果不确定时会直接抛出异常，不会自动重放可能已经完成的写操作。

隔离项目的 `adownload_files()` 默认每个文件最多 64 MiB；需要其他明确上限时，使用
`aread_bytes(..., max_bytes=...)`。命令响应最多 32 MiB，超出时会结束本次运行；大输出应使用文件捕获。

隔离项目的文件传输与命令使用同一次运行的边界。原始命令 Sandbox 的受限传输要求镜像提供 Python 3、
Linux procfs，并允许命令服务与文件服务共享进程视图。

## 接入 AgentRuntime

```python
from deepagents import FilesystemPermission

from tinkerfin import TinkerFin

permissions = [
    FilesystemPermission(
        operations=["write"],
        paths=["/policies/private/**"],
        mode="deny",
    )
]

runtime = (
    TinkerFin(checkpointer=checkpointer)
    .with_namespace("projects/project-a")
    .build(
        model=model,
        backend=project,
        permissions=permissions,
    )
)
```

Runtime 在运行开始时一起准备项目隔离访问和文件 middleware。权限规则需要 interrupt 而非 deny 时，
必须配置 checkpointer。只有由调用方自行管理的 Deep Agents Graph 才需要直接使用
`build_rooted_filesystem_middleware()`。

下一篇：[多进程持久化与自定义扩展](persistence-and-extensions.md)。
