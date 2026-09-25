# Sandbox 使用参考

[Sandbox 入门](index.md) · [English](../../en/sandbox/api-reference.md)

## 配置与连接

### `OpenSandboxConfig`

| 字段 | 默认值 | 作用 |
| --- | --- | --- |
| `image` | TinkerFin 固定版本镜像 | 新 Sandbox 使用的镜像 |
| `entrypoint` | `/opt/sandbox-runtime/bin/entrypoint.sh` | 容器入口命令 |
| `env` | `{}` | Sandbox 环境变量 |
| `metadata` | `{}` | 创建时附加的业务 metadata；保留字段不能覆盖 |
| `resource` | `cpu=1, memory=2Gi` | 资源规格 |
| `volumes` | `()` | OpenSandbox volume 配置 |
| `ttl` | 2 小时 | 从创建或续期起计算的生存时间，必须大于 0；`None` 表示手动清理 |
| `lifecycle_request_timeout` | 10 分钟 | SDK 未显式指定超时时使用的单次控制面请求时限 |
| `ready_timeout` | 5 分钟 | 等待新 Sandbox ready 的时限 |
| `connect_timeout` | 30 秒 | 连接数据面的时限 |
| `command_timeout` | 3600 秒 | 默认命令超时，必须大于等于 0 |
| `workspace_root` | `/workspace` | 文件工具映射的受限根；`None` 表示不创建 rooted view |
| `health_command` | `printf ok` | 健康检查命令 |
| `warm_pool_size` | `1` | 预热实例数，必须大于等于 0 |
| `command_env` | `{}` | 每次 Shell 命令附加的环境变量 |
| `enable_capture_offload` | `False` | 是否允许大输出写入文件 |

默认镜像固定到 [TinkerFin Sandbox Runtime](https://github.com/tinkerfin-ai/sandbox-runtime) 的不可变摘要，包含 Playwright 和无界面 Chromium。镜像配置只影响随后创建的远端实例，重连不会更新已有实例的运行环境。

`ttl=None` 创建的实例没有自动到期时间，Manager 不会为其续期，但仍执行健康检查并遵守 State
资源所有权规则。重连不会改变已有实例的到期时间。关闭和文件存储行为见[远端实例生存时间](lifecycle.md#远端实例生存时间)。

### `OpenSandboxClient`

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `connection_config` | 必填，可传 `None` | OpenSandbox 连接配置；`None` 时使用 SDK 环境配置 |
| `config` | `None` | TinkerFin Sandbox 配置 |
| `initializers` | `()` | 创建或连接完成后依次执行的幂等初始化函数 |

Client 使用 OpenSandbox SDK 0.1.16 和官方 Server 0.2.3。下表方法均为异步方法；Client 接收
远端实例 ID，Manager 接收应用 key。

| 方法 | 行为 |
| --- | --- |
| `create(metadata=None)` | 创建并初始化 backend，把所有权交给调用方 |
| `connect(sandbox_id)` | 连接并初始化已有实例 |
| `inspect(sandbox_id)` | 通过临时连接读取详情和执行健康状态；详情不可读时返回不可用快照 |
| `get_runtime_info(sandbox_id)` | 只读控制面详情，不查询端点、不初始化、不检查健康或续期 |
| `pause(sandbox_id)`、`resume(sandbox_id)` | 提交官方控制面状态变更；持有者协调和就绪检查由 Manager 负责 |
| `get_diagnostic_logs(sandbox_id, scope="container")` | 读取供应商日志诊断 |
| `get_diagnostic_events(sandbox_id, scope="runtime")` | 读取供应商事件诊断 |
| `destroy(sandbox_id)` | 幂等销毁实例，并关闭临时连接 |
| `aclose()` | 等待所属工作结束，只关闭由 Client 创建的 transport |

`get_runtime_info()` 读取失败时抛出 backend 错误。它的 `healthy=False` 表示未执行健康探测，
不能据此判断探测失败。底层 pause/resume 不会连接或初始化 backend；普通暂停、恢复流程应使用 Manager。

Client 禁用 SDK 隐式的 transport 重试。显式设置的 `ConnectionConfig.retry_policy` 会保留，
但启用 POST/PATCH 响应失败重放的配置会被 `ValueError` 拒绝。调用方传入的 transport 保留自身策略和
所有权；调用方配置、headers 和环境变量不会被修改。

初始化函数接收 `OpenSandboxBackend`，可以返回可等待对象或 `None`。I/O 应使用原生异步回调；
同步回调直接在事件循环执行，必须保持非阻塞，Client 不会把它移入线程。连接和初始化共用
`connect_timeout`；经 Manager 恢复时，还受更早的恢复截止时间约束。超时与取消依赖协作式执行，
不能打断阻塞的同步代码。初始化失败抛出
`OpenSandboxInitializationError`，不会触发恢复重试或重建。

## Manager

`OpenSandboxManager` 的构造参数和操作见 [Sandbox 生命周期](lifecycle.md)。
`workspace(key)` 返回供 `TinkerFin.build(backend=...)` 使用的惰性声明。
直接使用 Deep Agents 时，通过独立的 `build_rooted_filesystem_middleware(backend, ...)`
配置工作区文件工具。

`pause(key, timeout=30.0)` 在全部登记的持有者完成工作、且远端暂停已确认后返回 `None`。
`resume(key, timeout=30.0)` 返回同一实例的可用 backend。两者的工作预算均以秒计，必须为正有限数值。
`get_diagnostic_logs(key, scope="container")` 和 `get_diagnostic_events(key, scope="runtime")`
按范围读取诊断内容，不会唤醒实例。

只有配置的预热容量已经通过真实验证时，`await manager.check_ready()` 才会正常返回。启动或后台容量
失败会抛出 `OpenSandboxWarmPoolUnavailableError`，Manager 关闭后会抛出
`OpenSandboxManagerClosedError`。

可选生命周期通知契约：

| API | 用途 |
| --- | --- |
| `OpenSandboxLifecycleObserver.on_sandbox_event(event)` | 借用的异步观察者，不得重入当前 Manager |
| `OpenSandboxNotificationOptions(max_pending_events=128, timeout=1.0)` | 每个观察者独立的待投递上限和单次执行秒数 |
| `OpenSandboxLifecycleEvent` | 不可变的事件标识、类型、所有者、UTC 时间、原因、效果与可信诊断 |
| `OpenSandboxLifecycleEventType` | 用户 Sandbox 变化、显式工作区重置及独立的预热容量变化 |
| `OpenSandboxLifecycleReason` | 连接、健康、初始化、State 及显式操作的原因枚举 |

Manager 通过 `observers=()` 和 `notification_options=None` 配置通知。事件与交付语义见
[生命周期通知](lifecycle.md#生命周期通知)。`diagnostic_context` 仅供可信诊断，不属于客户端响应。

## Backend 和 handle

| API | 用途 |
| --- | --- |
| `OpenSandboxBackend` | 一条已连接的异步 OpenSandbox 数据面 |
| `OpenSandboxHandle` | 在远端实例替换后仍保持身份稳定的借用 handle |
| `RootedOpenSandboxBackend` | 把虚拟 `/` 映射到配置的 workspace root |
| `build_rooted_filesystem_middleware(...)` | 不使用 manager 时创建匹配的文件 middleware |

如果自定义 client 需要直接创建这些对象：`OpenSandboxBackend` 接收原生 `sandbox`、`default_timeout=60`、可选 `command_env`、可选 `working_directory`、`health_command="printf ok"` 和 `enable_capture_offload=False`；`OpenSandboxHandle` 接收 backend；`RootedOpenSandboxBackend` 接收 handle 和 `root="/workspace"`。

常用异步方法：

| 类别 | 方法 |
| --- | --- |
| Shell | `aexecute(command, timeout=None)` |
| 文件 | `aread`、`awrite`、`aedit`、`adelete`、`als`、`aglob`、`agrep` |
| 传输 | `aupload_files`、`adownload_files` |
| 大输出 | `aexecute_with_offload` |
| 生命周期 | `arenew(timeout)`、`aget_runtime_info()`、`akill()`、`aclose()` |

`RootedOpenSandboxBackend.to_shell_path(file_path)` 把虚拟路径转换成相对于工作区根目录的 Shell 路径，供 `aexecute` 中的命令使用。

同步远程方法会明确报错；始终使用 `a` 开头的异步版本。

## 状态实现

| API | 用途 |
| --- | --- |
| `OpenSandboxState` | 自定义绑定、租约、预热池、可用状态、持有者协调和清理协议 |
| `InMemoryOpenSandboxState(namespace="")` | 当前进程内状态 |
| `SQLAlchemyOpenSandboxState(...)` | 借用 `engine` 保存 SQLite、MySQL 或 PostgreSQL 共享状态 |
| `get_sqlalchemy_opensandbox_state_schema(dialect=...)` | 生成完整建表 SQL |
| `SQLAlchemyOpenSandboxStateSchema` | 不可变的 dialect、table names 和 DDL |

`OpenSandboxInitializer` 在创建或连接完成后接收可用 backend，返回 `Awaitable[None] | None`。
同步回调必须保持非阻塞。

### 不可变 claim 和 binding

| 类型 | 字段 |
| --- | --- |
| `OpenSandboxBinding` | `sandbox_id`、`generation` |
| `OpenSandboxOwnerClaim` | owner key、摘要、token、generation、可选 binding |
| `OpenSandboxWarmClaim` | slot、token、generation |
| `OpenSandboxReadyWarmClaim` | warm claim 字段和已发布 Sandbox ID |
| `OpenSandboxCleanupClaim` | sandbox ID、token、generation |

这些类型主要用于自定义 `OpenSandboxState`，普通 manager 使用者不需要手工创建。

### 可用状态与登记的持有者

| 类型 | 字段 |
| --- | --- |
| `OpenSandboxAvailability` | `owner_digest`、`sandbox_id`、`binding_generation`、`sequence`、`phase`、`connection_generation` |
| `OpenSandboxAvailabilityPhase` | `running`、`draining`、`pausing`、`paused`、`resuming`、`uncertain` |
| `OpenSandboxHolderUpdate` | `holder_id`、`owner_digest`、`sandbox_id`、`binding_generation`、`acknowledged_sequence`、`availability` |

自定义 State 还需提供以下异步操作：

| 方法 | 必须满足的行为 |
| --- | --- |
| `register_holder(claim, holder_id)` | 发布 handle 前，原子确认当前绑定可运行并登记 Manager |
| `read_availability(owner_key)` | 不等待所有者租约即可读取当前意图；`None` 表示没有绑定 |
| `get_holder_updates(holder_id)` | 批量返回持有者登记及其当前可用状态 |
| `change_availability(claim, expected, phase=..., refresh_connection=False)` | 仅变更与租约和预期快照完全一致的状态；进入 `pausing` 前必须收齐当前排空确认 |
| `acknowledge_idle(holder_id, availability)` | 为准确匹配的排空意图记录“已关闭准入且工作已结束” |
| `holders_are_idle(claim, availability)` | 要求全部登记持有者确认同一次排空 |
| `unregister_holder(holder_id, availability)` | 只在关闭准入、工作结束后移除准确匹配的绑定登记 |

`sequence` 随意图变更递增，`connection_generation` 在要求持有者重新连接时递增，两者都属于
同一 `binding_generation`。旧绑定和旧序号不能确认或修改后续状态。持有者 ID 标识一次 Manager
生命周期，必须非空且不超过 36 个字符。worker 到期、心跳丢失或 State 关闭都不能替代空闲确认。
存储失败必须抛出 State 错误，不能据此允许新工作进入。

## 运行信息

| 模型 | 主要字段 |
| --- | --- |
| `OpenSandboxStatusInfo` | `state`、可选 reason/message/last transition time |
| `OpenSandboxPlatformInfo` | `os`、`arch` |
| `OpenSandboxRuntimeInfo` | Sandbox ID、available、healthy、状态、时间、镜像、平台、metadata、不可用原因 |
| `OpenSandboxDetails` | RuntimeInfo 加 `owner_key`、`cached` 和可选 `access_state` |
| `OpenSandboxUnavailableReason` | `not_found` 或 `unreachable` |

`access_state` 表示框架的共享协调阶段，可为 `running`、`draining`、`pausing`、`paused`、`resuming`
或 `uncertain`；`None` 表示未提供协调状态快照。`manager.get_details()` 会同时读取 State 和供应商详情。
远端 `status.state="Running"` 可能与 `access_state="draining"` 或未确认的生命周期请求并存。
`cached` 只表示本地存在 handle，不能据此判断它当前允许接收工作。

### 诊断内容

`OpenSandboxDiagnosticContent` 不可变，包含 `sandbox_id`、`kind`（`logs` 或 `events`）、`scope`、
`delivery`（`inline` 或 `url`）、`content_type`、`truncated` 和 `warnings`。内联结果包含 `content`；
URL 结果包含 `content_url` 和 `expires_at`。可选 `content_length` 以字节计。
`warnings` 说明来源缺失或留存缺口，没有警告时为空元组。

Docker 支持 `container`/`all` 日志范围和 `runtime`/`all` 事件范围。事件内容是当前状态摘要，
不构成完整事件历史。框架不会自动下载返回的 URL；诊断正文和引用面向可信运维使用者，访问由宿主控制。

## 错误

| 错误 | 含义 |
| --- | --- |
| `OpenSandboxStateError` | 状态层错误基类 |
| `OpenSandboxStateOwnershipError` | claim 已过期、被替换或不属于当前 worker |
| `OpenSandboxStateConfigurationError` | 状态配置、数据库或 schema 不支持 |
| `OpenSandboxDestroyError` | 远端销毁未能可靠完成 |
| `OpenSandboxInitializationError` | 工作区准备或初始化函数失败，不适用恢复重试 |
| `OpenSandboxBackendUnavailableError` | 原实例恢复失败，或供应商拒绝访问 |
| `OpenSandboxBackendTimeoutError` | 连接、暂停、恢复或其他 backend 操作的工作预算耗尽 |
| `OpenSandboxPausedError` | 数据面访问需要先显式恢复 |
| `OpenSandboxBusyError` | 待执行的暂停正在等待已有操作结束 |
| `OpenSandboxLifecycleUncertainError` | 生命周期请求结果尚未确认，不能安全地重新开放访问 |
| `OpenSandboxResetError` | workspace 无安全根或重置失败 |
| `OpenSandboxHandleOwnershipError` | 使用了不再有效的 backend 所有权 |
| `OpenSandboxManagerClosedError` | manager 关闭后仍被使用 |
| `OpenSandboxObserverReentryError` | 生命周期观察者尝试操作或关闭正在向其投递的 Manager |
| `OpenSandboxSettlementTimeoutError` | 调用方等待安全关闭超过时限 |
