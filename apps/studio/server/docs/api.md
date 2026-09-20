# Studio HTTP API

[返回后端使用说明](../README.md)

## HTTP 响应

`/api` 下普通业务 JSON 接口成功时统一返回 HTTP 200 和
`{ "code": 0, "message": "success", "data": ... }`。保存、删除和登出等不返回业务数据的操作使用 `data: null`。
错误使用相同包络并保留对应的 HTTP 状态码，客户端应同时读取 HTTP 状态和业务 `code`。
附件上传和读取许可使用相同 JSON 包络，会话与 Trace 订阅返回原生 SSE；`/health/live` 和 `/health/ready` 使用独立的健康检查响应。

## 认证会话

访问令牌从签发时刻起使用固定有效期，请求和用户操作不会延长到期时间。`POST /api/auth/login` 返回访问令牌、UTC `expires_at`
和用户信息；`GET /api/auth/me` 返回同一 `expires_at` 和当前用户。过期、撤销或无效令牌统一返回 401。

登录、`/api/auth/me` 和用户查询响应中的 `avatar_url` 为可空 HTTPS 头像地址。
`PATCH /api/user/me` 修改当前登录用户资料：`display_name` 必须是 1～128 个字符，
`avatar_url` 最大 2048 个字符且必须使用 HTTPS，传入 `null` 可清空头像。

## 对话请求边界

`POST /api/conversation/chat` 接收标准 AG-UI `RunAgentInput`，并返回可回放的 AG-UI SSE。请求会校验模型、会话归属、请求模式和恢复参数。

| 字段 | Studio 的处理方式 |
| --- | --- |
| `threadId` | 空字符串表示新会话；已有值必须属于当前用户 |
| `runId` | 新输入使用新值；同一次网络重试或附着复用原值 |
| `parentRunId` | 保留在标准请求快照中，不改变会话或子智能体的归属关系 |
| `state` | 保留在标准请求快照中，不自动成为 Graph 输入 |
| `messages` | 普通运行接受一条文字及附件 user 消息，也可仅附件；resume 必须为空 |
| `tools` | 仅保留客户端工具描述，不授予服务端工具执行权限 |
| `context` | 保留在标准请求快照中，由业务决定是否使用 |
| `forwardedProps` | `model` 与 `command.plan` 必填；Plan 只接受 `on/off`，未知 command 与其他扩展字段完整保留 |
| `resume` | 提交待处理交互的公开 ID 和决策；必须完整覆盖当前待处理项，并与对应工具调用匹配 |

每条标准 AG-UI 消息必须带非空客户端 ID。该 ID 只用于通过 HTTP 协议校验，不参与权限、幂等、
执行关联或持久化身份；服务端会分配实际使用的消息 ID。
`RUN_STARTED` 返回 服务端确认的 `threadId`、`runId`、标题及其来源、生成状态和递增 `titleSeq`，不携带 input。

会话标题最多 32 个 Unicode 字符，临时标题截取用户输入前 16 个字符。自动总结独立于聊天响应，主回复结束或客户端断连后继续生成。`GET /conversation/{threadId}/title` 返回当前用户会话的标题快照，包含 `threadId`、`title`、`titleSource`、`titleGenerationStatus`、`titleSeq`。客户端按 `titleSeq` 应用较新的标题；历史查询也返回已保存的标题。手动保存标题后，自动生成不能覆盖。

`forwardedProps.command.plan` 必须为 `on` 或 `off`，用于开启或关闭计划模式。
不接受 `forwardedProps.mode`；`command` 中的其他字段会保留，但当前服务端只处理 `plan`。
同一次运行重试、分支和恢复必须保持模型一致。

主智能体与 Studio 研究子智能体每次执行最多允许 24 个新工具调用，成功和失败均计入；超出时结束并返回限制说明。新消息或用户审批恢复开始新的预算，恢复时已有的待执行调用不重复计数。预期工具失败允许模型调整方案，服务端不会自动重试工具；未知异常仍使运行失败。

## 个人模型与附件

`GET /api/models/settings` 返回本人的提供方连接、模型配置和提供方预设。连接通过 `/api/models/connections/{connection_id}` 保存或删除，保存时请求体的 `connection_id` 必须与路径一致。密钥归属连接，响应只返回 `has_key`；同一地址更新时 `api_key: null` 保留已有密钥，更改地址须重新提供。`auth_type: "none"` 表示无需认证。

`/api/models/configurations` 管理模型配置，每项通过 `connection_id` 引用本人的连接；`POST` 接收 1～200 项的数组，`PUT /{model_id}` 保存单项。`GET /api/models` 返回本人启用的对话模型。对话和生图分别选择默认项；生图需要已配置且启用的默认模型。

`PUT /api/models/configurations/{model_id}/default` 不接收请求体，启用目标模型并切换本人同一用途的默认选择。连接须满足其认证要求；该操作不修改连接、密钥或生成参数。

`POST /api/models/connections/{connection_id}/models` 查询连接的可用模型，不保存或启用返回项。
`POST /api/models/configurations/test` 接收 `{"kind":"basic","configuration":{...}}`，`configuration` 使用模型保存接口的字段并引用已保存的本人连接；测试不会保存配置。`kind` 可选 `basic`、`text`、`vision`、`image`，总超时分别为 10、30、45、120 秒。

测试结果位于 `ApiResponse.data`，包含 `kind`、`outcome`（`success`、`failed`、`inconclusive`）、`elapsed_ms`、`code`、可空的 `text` 与 `image`。图片包含 `mime_type` 和 `data_base64`；视觉测试返回测试图和实际回复，生图测试返回经校验、限额的预览。基础检查无法取得模型列表时返回未确认，不据此否定模型能力。供应商错误转为安全错误码，不返回密钥或供应商原始错误正文。测试不自动重试、不修改能力标记、不创建会话附件，能力调用可能产生供应商费用。

`generation_options` 是最多 64 KiB、64 层容器嵌套的严格 JSON 对象，数值必须有限；顶层不能覆盖 `model`、`prompt`、`n`、`api_key` 或 `authorization`（忽略大小写）。`size` 和 `output_format` 如提供则必须为非空字符串，其他供应商字段保留并交给生图接口。

公网模型默认使用 HTTPS。接入 Ollama 或内网模型时，管理员在后端环境配置中设置 `MODEL_ALLOWED_ORIGINS`，列出允许访问的准确协议、主机与端口：

```dotenv
MODEL_ALLOWED_ORIGINS=["http://127.0.0.1:11434","http://localhost:11434"]
```

重新启动后端后，连接选择 Ollama 原生 API（`api_type: "ollama"`），Base URL 填写 `http://127.0.0.1:11434`，无需认证时设置 `auth_type: "none"`。模型名称填写 Ollama 已安装的模型；图片输入和工具调用取决于模型能力。

允许列表不包含 `/v1`、查询参数、账户信息或通配符。HTTP、本机及内网访问按协议、主机和端口精确匹配，不因域名指向同一 IP 而自动互相授权。该设置适用于所有用户的聊天、生图和生成图片下载；允许的 HTTP 服务应位于受信任网络。更改设置后需重启后端。

本机地址指 Studio 后端所在的网络空间。Docker 部署访问宿主机 Ollama 时，在 `deploy/.env` 中填写 `MODEL_ALLOWED_ORIGINS=["http://host.docker.internal:11434"]`，模型 Base URL 使用 `http://host.docker.internal:11434`；Ollama 需监听容器可达的地址。连接另一台服务器时，使用该服务器可达的主机名或 IP 并添加对应来源。

附件上传和读取流程：

1. `POST /api/attachments/uploads` 提交 `{"name":"report.md","size_bytes":128}`，取得 `data` 中的 `attachment_id`、`url`、`fields` 和 `expires_in`。
2. 浏览器向 `url` 提交 `multipart/form-data`：先原样添加全部 `fields`，最后添加名为 `file` 的文件字段；不携带 Studio 令牌或 Cookie。上传许可默认有效 600 秒，仅允许指定文件大小。
3. 直传成功后调用 `POST /api/attachments/{attachment_id}/complete`，后端校验内容并返回 `Attachment`。取得此结果后才能发送消息或保存任务；文件传输完成不等于附件已就绪。处理中的重复请求返回 409，已完成请求返回同一附件。
4. `GET /api/attachments/{id}/download-url?variant=original` 返回 `data.url` 和 `data.expires_in`，浏览器直接读取文件；图片预览使用 `variant=preview`。链接默认有效 300 秒，过期后重新申请，不保存到消息或公开分享。
5. `DELETE /api/attachments/{id}` 删除本人未发送草稿；已发送或被任务引用的附件随所属记录保留。

图片及文档输入使用 AG-UI 内容块，`source.value` 为 `attachment:<id>`；附件必须属于当前用户，metadata 以已保存的附件描述为准。消息文本仍受 UTF-8 大小限制。

附件存储配置见[服务端部署说明](../README.md)。未发送附件保留至少 24 小时；需要长期使用的文件应随消息发送或保存到自动化任务。

## 会话历史与事件

- `GET /api/conversation/history` 只读取 Studio 列表摘要
- `GET /api/conversation/{threadId}/history` 在校验用户归属后返回固定 `asOfSeq` 的 Trace 视图；
  `historyCursor` 只扩展同一固定前缀的 Turn 窗口；`includeTaskTrace=true` 会从同一 Trace 前缀
  返回根 Agent 任务轨迹，`false` 省略任务轨迹
- `GET /api/conversation/{threadId}/trace` 先发送完整 Trace snapshot，再按提交顺序发送语义增量；
  `includeTaskTrace=true` 时只在任务轨迹实际变化后发送完整 replacement；关闭订阅会停止本次跟随
- Trace 视图和增量携带 `generation`、`asOfSeq`、`observedAt`。同一代按事件序号和存储 UTC
  观测时间排序，保留微秒；writer 失活或有效接管可以在同一序号更新运行状态。列表摘要用相同
  规则拒绝迟到快照；相同观测发生内容冲突时重新读取 Trace。历史分页保留固定前缀的原始观测，
  补充历史内容时保留前端已收到的较新运行状态
- `GET /api/conversation/{threadId}/trace/graph` 在校验用户归属后筛选链路节点，支持 `kind`、`status`、`modelCallId`、`agent`、`provider`、`model`、`graph_namespace`、
  `query`、`startedAfter`、`startedBefore`、opaque `cursor` 与 `limit`；响应中的 Turn 是容器，
  `matchedNodeIds` 只包含直接命中，响应会补入直接命中所属的 Subagent 链
- `GET /api/conversation/{threadId}/trace/graph/follow` 先发送同一筛选首页，再持续发送节点
  和 Turn 的 upsert/remove、完整 `orderedNodeIds`、`matchedNodeIds`、`nextCursor`、`asOfSeq`
  与 `completeness`；关闭订阅会停止本次跟随
- `POST /api/conversation/chat` 返回当前运行的 AG-UI 事件；终态会话正文仍以 Trace 为准

会话详情和 Trace 初始快照必须包含 `runFailures`；实时更新事件在外层携带同名数组，
与 `update` 并列。数组来自同一固定前缀中的公开运行事实，仅包含当前历史窗口内普通提问的
失败记录；分页扩展时客户端按 `runId` 合并，不能通过缺少助手消息推断失败。
每项包含 `runId`、可空的 `errorCode`、UTC 时间 `failedAt` 和 `retryable`。
只有错误码为 `runtime_initialization_error` 的执行前失败可重新发送；取消、成功和恢复操作不会生成普通提问失败记录。

“重试”使用普通 `POST /api/conversation/chat`，携带新的运行和用户消息 ID；
输入为原问题及原附件引用，使用当前上下文、模型和模式。它不会恢复或修改原运行，也不修改链路。

Trace Graph 中，同一 Turn 作用域的节点按真实开始序号平级排列，只有 Subagent 形成嵌套。
`parentSubagentId` 是唯一展示嵌套关系；`modelCallId` 只关联 Assistant、Tool、Subagent 与产生它的
Model。Assistant 没有可见正文但对应 Model 确实发出 Tool 调用时，`toolCallOnly` 为 `true`，且不受
API 查询是否返回 Tool 节点影响。非空 `graphNamespace` 只表示 Graph 作用域，必须有经过校验的 Subagent 来源才能形成嵌套。
节点以深度优先顺序返回，子智能体嵌套最多 64 层。
