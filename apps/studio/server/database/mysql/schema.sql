SET NAMES utf8mb4 COLLATE utf8mb4_0900_ai_ci;

CREATE TABLE users (
  id INTEGER NOT NULL AUTO_INCREMENT COMMENT '用户主键',
  username VARCHAR(64) NOT NULL COMMENT '登录用户名',
  display_name VARCHAR(128) NOT NULL COMMENT '展示名称',
  avatar_url VARCHAR(2048) COMMENT '头像 HTTPS URL',
  password_hash VARCHAR(512) NOT NULL COMMENT '带算法、参数和独立盐值的密码哈希',
  roles JSON NOT NULL COMMENT '用户角色列表',
  disabled BOOL NOT NULL COMMENT '是否禁止登录',
  CONSTRAINT pk_users PRIMARY KEY (id),
  UNIQUE KEY ix_users_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='TinkerFin Studio 登录用户';

CREATE TABLE model_connections (
  id INTEGER NOT NULL COMMENT '连接记录主键' AUTO_INCREMENT,
  user_id INTEGER NOT NULL COMMENT '连接所属用户 ID',
  connection_id VARCHAR(64) NOT NULL COMMENT '用户范围内稳定的连接 ID',
  display_name VARCHAR(128) NOT NULL COMMENT '用户自定义连接显示名称',
  provider_id VARCHAR(64) NOT NULL COMMENT '提供方目录标识，custom 表示自定义',
  api_type VARCHAR(32) NOT NULL COMMENT 'API 类型：openai_chat_completions 或 ollama',
  base_url VARCHAR(1024) NOT NULL COMMENT '模型 API 基础地址，Ollama 为服务根地址',
  auth_type VARCHAR(16) NOT NULL COMMENT 'api_key 密钥认证或 none 无需认证',
  api_key TEXT NOT NULL COMMENT '服务明文密钥，禁止通过响应或日志暴露',
  created_at DATETIME NOT NULL COMMENT 'UTC 创建时间',
  updated_at DATETIME NOT NULL COMMENT 'UTC 更新时间',
  PRIMARY KEY (id),
  CONSTRAINT uq_model_connections_owner_connection UNIQUE (user_id, connection_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='用户模型服务的地址与认证';

CREATE TABLE agent_models (
  id INTEGER NOT NULL COMMENT '模型配置主键' AUTO_INCREMENT,
  generation_options JSON NOT NULL COMMENT '生图接口附加参数，不包含认证信息',
  user_id INTEGER NOT NULL COMMENT '模型配置所属用户 ID',
  purpose VARCHAR(16) NOT NULL COMMENT 'chat 对话模型或 image 生图服务' DEFAULT 'chat',
  model_id VARCHAR(64) NOT NULL COMMENT '前后端使用的稳定模型 ID',
  display_name VARCHAR(128) NOT NULL COMMENT '前端展示名称',
  connection_id VARCHAR(64) NOT NULL COMMENT '所属连接 ID，与 user_id 共同确定连接',
  chat_options JSON NOT NULL COMMENT '聊天生成参数，不包含连接或认证信息',
  model_name VARCHAR(128) NOT NULL COMMENT '供应商实际模型名称',
  image_support VARCHAR(16) NOT NULL COMMENT '图片输入能力：supported、unsupported 或 unknown' DEFAULT 'unknown',
  reasoning_enabled BOOL NOT NULL COMMENT '是否启用已验证的 provider reasoning 参数',
  enabled BOOL NOT NULL COMMENT '是否允许创建新 run',
  is_default BOOL NOT NULL COMMENT '是否为前端默认模型，由应用事务保证唯一',
  sort_order INTEGER NOT NULL COMMENT '模型目录升序排序值',
  created_at DATETIME NOT NULL COMMENT '创建时间',
  updated_at DATETIME NOT NULL COMMENT '更新时间',
  PRIMARY KEY (id),
  CONSTRAINT uq_agent_models_owner_model UNIQUE (user_id, model_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='可由前端选择的 Agent 模型与连接配置';

CREATE INDEX ix_agent_models_connection ON agent_models (user_id, connection_id);

CREATE INDEX ix_agent_models_default ON agent_models (user_id, purpose, is_default, enabled);

CREATE INDEX ix_agent_models_enabled_order ON agent_models (user_id, enabled, sort_order, id);

CREATE TABLE conversation_threads (
  id BIGINT NOT NULL AUTO_INCREMENT COMMENT '会话主键',
  user_id BIGINT NOT NULL COMMENT '所属用户 ID，由应用层保证存在',
  thread_id VARCHAR(128) NOT NULL COMMENT '公开 AG-UI 与 Trace 共用的 threadId',
  title VARCHAR(32) NOT NULL COMMENT '会话标题，最多32个字符',
  title_source VARCHAR(16) NOT NULL DEFAULT 'default' COMMENT '标题来源：default 临时、generated 总结、user 手动、unknown 未记录',
  title_generation_status VARCHAR(16) NOT NULL DEFAULT 'idle' COMMENT '标题生成状态：idle 未尝试、running 已认领、succeeded 成功、failed 失败、skipped 跳过',
  title_seq INT NOT NULL DEFAULT 0 COMMENT '标题及生成状态每次提交递增的序号，与 Trace 序号无关',
  status VARCHAR(32) NOT NULL COMMENT '列表状态：idle/running/waiting_approval/error/deleting',
  last_run_id VARCHAR(128) COMMENT '最近主 Run ID',
  last_access_mode VARCHAR(32) NOT NULL DEFAULT 'full' COMMENT '最近主运行文件审批模式：full 或 write_approval',
  last_model VARCHAR(64) COMMENT '最近主 Run 使用的稳定模型 ID',
  message_count INTEGER NOT NULL COMMENT 'Trace 中 user 与 assistant 消息数',
  tool_call_count INTEGER NOT NULL COMMENT 'Trace 中 Tool proposal 数',
  has_pending_interrupt BOOL NOT NULL COMMENT 'Trace 是否存在待处理交互',
  pending_interaction_kind VARCHAR(64) COMMENT 'Trace 待处理交互的产品类型',
  pinned BOOL NOT NULL COMMENT '是否置顶',
  created_at DATETIME NOT NULL COMMENT '创建时间',
  updated_at DATETIME NOT NULL COMMENT '最近 Trace 或用户会话活动时间',
  deleted_at DATETIME COMMENT '软删除时间',
  CONSTRAINT pk_conversation_threads PRIMARY KEY (id),
  CONSTRAINT uq_conversation_threads_thread UNIQUE (thread_id),
  KEY ix_conversation_threads_user_pinned_updated (user_id, deleted_at, pinned, updated_at, id),
  KEY ix_conversation_threads_user_status_updated (user_id, status, updated_at, id),
  KEY ix_conversation_threads_user_updated (user_id, deleted_at, updated_at, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='用户会话归属、产品控制与 Trace 列表摘要';

CREATE TABLE conversation_run_registrations (
  id BIGINT NOT NULL AUTO_INCREMENT COMMENT 'Run 注册主键',
  conversation_thread_id BIGINT NOT NULL COMMENT '所属会话主键，由应用层保证存在',
  run_id VARCHAR(128) NOT NULL COMMENT '公开且幂等的主 Run ID',
  parent_run_id VARCHAR(128) COMMENT 'branch 或 resume 来源 Run ID',
  access_mode VARCHAR(32) NOT NULL DEFAULT 'full' COMMENT '本运行固定的文件审批模式：full 或 write_approval',
  model_id VARCHAR(64) NOT NULL COMMENT '主 Run 使用的稳定模型 ID',
  status VARCHAR(32) NOT NULL COMMENT 'preparing/starting/running/waiting/succeeded/failed/cancelled/abandoned',
  input_json JSON NOT NULL COMMENT '用于同 runId 幂等核验的标准请求',
  terminal_outcome VARCHAR(32) COMMENT 'Trace 终态结果',
  error_code VARCHAR(128) COMMENT '客户端安全的终态错误码',
  trace_generation VARCHAR(2048) COMMENT '首次观测绑定的 Trace generation',
  trace_as_of_seq BIGINT COMMENT '列表摘要已消费的 Trace 事件前缀',
  trace_observed_at DATETIME(6) COMMENT 'Trace 存储观测时间，UTC 微秒，用于同前缀状态排序',
  started_at DATETIME NOT NULL COMMENT '请求注册时间',
  finished_at DATETIME COMMENT 'Trace 终态时间',
  created_at DATETIME NOT NULL COMMENT '创建时间',
  updated_at DATETIME NOT NULL COMMENT '最近业务状态更新时间',
  CONSTRAINT pk_conversation_run_registrations PRIMARY KEY (id),
  CONSTRAINT uq_conversation_run_registrations_thread_run UNIQUE (conversation_thread_id, run_id),
  KEY ix_conversation_run_registrations_thread_started (conversation_thread_id, started_at, id),
  KEY ix_conversation_run_registrations_thread_status (conversation_thread_id, status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='主 Run 请求幂等、模型与业务状态注册';

CREATE TABLE conversation_interrupt_claims (
  id BIGINT NOT NULL AUTO_INCREMENT COMMENT '认领主键',
  conversation_thread_id BIGINT NOT NULL COMMENT '所属会话主键，由应用层保证存在',
  interrupt_id VARCHAR(255) NOT NULL COMMENT '框架从 Checkpointer 解析的公开 interrupt ID',
  source_run_id VARCHAR(128) NOT NULL COMMENT '产生 interrupt 的来源 Run ID',
  claimed_run_id VARCHAR(128) NOT NULL COMMENT '原子认领该 interrupt 的 resume Run ID',
  status VARCHAR(32) NOT NULL COMMENT 'claimed/resolved/cancelled',
  resolution_id VARCHAR(128) COMMENT 'checkpoint marker 或 Trace abandonment 证据',
  created_at DATETIME NOT NULL COMMENT '认领创建时间',
  resolved_at DATETIME COMMENT '框架确认恢复 checkpoint 的时间',
  updated_at DATETIME NOT NULL COMMENT '最近状态更新时间',
  CONSTRAINT pk_conversation_interrupt_claims PRIMARY KEY (id),
  CONSTRAINT uq_conversation_interrupt_claims_thread_interrupt UNIQUE (conversation_thread_id, interrupt_id),
  KEY ix_conversation_interrupt_claims_run_status (conversation_thread_id, claimed_run_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='由框架恢复事实驱动的 interrupt 原子认领与结算';

CREATE TABLE conversation_attachments (
	id VARCHAR(32) NOT NULL COMMENT '服务端随机附件 ID，同时作为不透明存储标识',
	user_id INTEGER NOT NULL COMMENT '附件所属用户 ID',
	thread_id VARCHAR(128) COMMENT '附件绑定的会话 ID；空值表示未发送草稿',
	message_id VARCHAR(128) COMMENT '首次使用或生成附件的权威消息 ID',
	name VARCHAR(255) NOT NULL COMMENT '用户可见文件名，不用于存储路径',
	mime_type VARCHAR(128) NOT NULL COMMENT '服务端验证的文件媒体类型；待确认上传为空',
	size_bytes BIGINT NOT NULL COMMENT '原件大小，单位为字节；待确认上传为声明值',
	sha256 VARCHAR(64) NOT NULL COMMENT '原件完整内容的 SHA-256 校验值',
	status VARCHAR(16) NOT NULL COMMENT 'uploading、processing、ready 或 deleting；仅 ready 可用',
	source VARCHAR(16) NOT NULL COMMENT 'user 表示用户上传，tool 表示工具生成',
	created_at DATETIME NOT NULL COMMENT 'UTC 创建时间，用于未发送附件的清理',
	PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='会话上传和生成附件的持久化引用';
CREATE INDEX ix_conversation_attachments_cleanup ON conversation_attachments (thread_id, created_at);
CREATE INDEX ix_conversation_attachments_owner ON conversation_attachments (user_id, thread_id);

CREATE TABLE attachment_collections (
	id VARCHAR(36) NOT NULL COMMENT '集合稳定ID，任务配置由请求ID推导，运行使用执行ID',
	user_id INTEGER NOT NULL COMMENT '所属用户ID',
	purpose VARCHAR(16) NOT NULL COMMENT 'input为不可变任务配置，execution为运行附件',
	task_id VARCHAR(36) COMMENT '关联自动化任务ID，删除任务后仍保留历史引用',
	configuration JSON NOT NULL COMMENT '不含凭据的不可变任务或运行输入快照',
	created_at DATETIME NOT NULL COMMENT 'UTC创建时间',
	PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='自动化任务配置和运行附件的持久归属';

CREATE INDEX ix_attachment_collections_owner_task ON attachment_collections (user_id, task_id);

CREATE TABLE attachment_references (
	collection_id VARCHAR(36) NOT NULL COMMENT '附件集合ID',
	attachment_id VARCHAR(32) NOT NULL COMMENT '附件文件ID',
	PRIMARY KEY (collection_id, attachment_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='自动化附件集合与文件引用，由服务校验归属';

CREATE INDEX ix_attachment_references_file ON attachment_references (attachment_id);

INSERT INTO users (username, display_name, avatar_url, password_hash, roles, disabled)
VALUES ('tinkerfin', 'TinkerFin', NULL, '$pbkdf2-sha256$600000$1ZFendL8broCk5OyW_zBQA$FHwGHqHzxz2n3-ksjSWYySw1tq6sE3X83sIsAGB1REI', '[]', FALSE);
