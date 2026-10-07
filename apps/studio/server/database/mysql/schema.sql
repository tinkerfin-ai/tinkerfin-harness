SET NAMES utf8mb4 COLLATE utf8mb4_0900_ai_ci;

CREATE TABLE users (
  id INTEGER NOT NULL AUTO_INCREMENT COMMENT '用户主键',
  username VARCHAR(64) NOT NULL COMMENT '登录用户名',
  display_name VARCHAR(128) NOT NULL COMMENT '展示名称',
  avatar_url VARCHAR(2048) COMMENT '头像地址（HTTPS）',
  password_hash VARCHAR(512) NOT NULL COMMENT '密码哈希（含算法、参数和盐值）',
  roles JSON NOT NULL COMMENT '用户角色列表',
  disabled BOOL NOT NULL COMMENT '是否禁止登录',
  CONSTRAINT pk_users PRIMARY KEY (id),
  UNIQUE KEY ix_users_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='登录用户';

CREATE TABLE model_connections (
  id INTEGER NOT NULL COMMENT '连接记录 ID' AUTO_INCREMENT,
  user_id INTEGER NOT NULL COMMENT '连接所属用户 ID',
  connection_id VARCHAR(64) NOT NULL COMMENT '连接 ID',
  display_name VARCHAR(128) NOT NULL COMMENT '连接显示名称',
  provider_id VARCHAR(64) NOT NULL COMMENT '模型服务提供方标识，custom 表示自定义',
  api_type VARCHAR(32) NOT NULL COMMENT '接口类型：openai_chat_completions、ollama',
  base_url VARCHAR(1024) NOT NULL COMMENT '模型服务基础地址',
  auth_type VARCHAR(16) NOT NULL COMMENT '认证方式：api_key 密钥认证、none 无需认证',
  api_key TEXT NOT NULL COMMENT '服务密钥（明文）',
  created_at DATETIME NOT NULL COMMENT 'UTC 创建时间',
  updated_at DATETIME NOT NULL COMMENT 'UTC 更新时间',
  PRIMARY KEY (id),
  CONSTRAINT uq_model_connections_owner_connection UNIQUE (user_id, connection_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='模型服务连接配置';

CREATE TABLE agent_models (
  id INTEGER NOT NULL COMMENT '模型配置 ID' AUTO_INCREMENT,
  user_id INTEGER NOT NULL COMMENT '模型配置所属用户 ID',
  model_id VARCHAR(64) NOT NULL COMMENT '模型 ID',
  display_name VARCHAR(128) NOT NULL COMMENT '模型显示名称',
  connection_id VARCHAR(64) NOT NULL COMMENT '所属连接 ID',
  chat_options JSON NOT NULL COMMENT '聊天生成参数',
  model_name VARCHAR(128) NOT NULL COMMENT '服务提供方模型名称',
  image_support VARCHAR(16) NOT NULL COMMENT '图片输入能力：supported 支持、unsupported 不支持、unknown 未知' DEFAULT 'unknown',
  reasoning_enabled BOOL NOT NULL COMMENT '是否启用模型推理',
  enabled BOOL NOT NULL COMMENT '是否启用模型',
  is_default BOOL NOT NULL COMMENT '是否为默认模型',
  sort_order INTEGER NOT NULL COMMENT '模型排序值（升序）',
  created_at DATETIME NOT NULL COMMENT 'UTC 创建时间',
  updated_at DATETIME NOT NULL COMMENT 'UTC 更新时间',
  PRIMARY KEY (id),
  CONSTRAINT uq_agent_models_owner_model UNIQUE (user_id, model_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='模型配置';

CREATE INDEX ix_agent_models_connection ON agent_models (user_id, connection_id);

CREATE INDEX ix_agent_models_default ON agent_models (user_id, is_default, enabled);

CREATE INDEX ix_agent_models_enabled_order ON agent_models (user_id, enabled, sort_order, id);

CREATE TABLE service_configs (
  id VARCHAR(64) NOT NULL COMMENT '服务配置 ID',
  user_id INTEGER NOT NULL COMMENT '配置所属用户 ID',
  capability VARCHAR(32) NOT NULL COMMENT '服务类型：web_search 网页搜索、image_generation 图片生成',
  provider_id VARCHAR(32) NOT NULL COMMENT '服务提供方：tavily、openai、fal、custom',
  enabled BOOL NOT NULL COMMENT '是否启用服务',
  config JSON NOT NULL COMMENT '服务请求与结果配置',
  api_key TEXT NOT NULL COMMENT '服务密钥（明文）',
  test_status VARCHAR(16) COMMENT '最近测试状态：success 成功、failed 失败',
  test_code VARCHAR(32) COMMENT '最近测试结果码',
  test_fingerprint VARCHAR(64) COMMENT '最近测试的配置摘要',
  tested_at DATETIME COMMENT 'UTC 最近测试时间',
  created_at DATETIME NOT NULL COMMENT 'UTC 创建时间',
  updated_at DATETIME NOT NULL COMMENT 'UTC 更新时间',
  PRIMARY KEY (id, user_id),
  CONSTRAINT uq_service_configs_owner_capability UNIQUE (user_id, capability)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='搜索与图片生成服务配置';

CREATE INDEX ix_service_configs_owner_enabled ON service_configs (user_id, enabled);

CREATE TABLE projects (
  id VARCHAR(36) NOT NULL COMMENT '项目 ID',
  user_id BIGINT NOT NULL COMMENT '所属用户 ID',
  name VARCHAR(64) COLLATE utf8mb4_bin NOT NULL COMMENT '项目名称',
  created_at DATETIME NOT NULL COMMENT 'UTC 创建时间',
  updated_at DATETIME NOT NULL COMMENT 'UTC 更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uq_projects_user_name (user_id, name),
  KEY ix_projects_user_created (user_id, created_at, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='用户项目';

CREATE TABLE conversation_threads (
  project_id VARCHAR(36) NOT NULL COMMENT '所属项目 ID',
  archived BOOLEAN NOT NULL DEFAULT 0 COMMENT '是否归档',
  KEY ix_conversation_threads_project_history (user_id, project_id, archived, deleted_at, updated_at, id),
  id BIGINT NOT NULL AUTO_INCREMENT COMMENT '会话主键',
  user_id BIGINT NOT NULL COMMENT '所属用户 ID',
  thread_id VARCHAR(128) NOT NULL COMMENT '会话 ID',
  title VARCHAR(32) NOT NULL COMMENT '会话标题',
  title_source VARCHAR(16) NOT NULL DEFAULT 'default' COMMENT '标题来源：default 默认、generated 自动生成、user 手动、unknown 未知',
  title_generation_status VARCHAR(16) NOT NULL DEFAULT 'idle' COMMENT '标题生成状态：idle 未开始、running 生成中、succeeded 成功、failed 失败、skipped 跳过',
  title_seq INT NOT NULL DEFAULT 0 COMMENT '标题更新序号',
  status VARCHAR(32) NOT NULL COMMENT '会话状态：idle、running、waiting_approval、error、deleting',
  last_run_id VARCHAR(128) COMMENT '最近主运行 ID',
  last_access_mode VARCHAR(32) NOT NULL DEFAULT 'full' COMMENT '最近主运行文件审批模式：full、write_approval',
  last_model VARCHAR(64) COMMENT '最近主运行模型 ID',
  message_count INTEGER NOT NULL COMMENT '用户与助手消息总数',
  tool_call_count INTEGER NOT NULL COMMENT '工具调用数',
  has_pending_interrupt BOOL NOT NULL COMMENT '是否有待处理交互',
  pending_interaction_kind VARCHAR(64) COMMENT '待处理交互类型',
  pinned BOOL NOT NULL COMMENT '是否置顶',
  created_at DATETIME NOT NULL COMMENT 'UTC 创建时间',
  updated_at DATETIME NOT NULL COMMENT 'UTC 最近会话活动时间',
  deleted_at DATETIME COMMENT 'UTC 删除时间',
  CONSTRAINT pk_conversation_threads PRIMARY KEY (id),
  CONSTRAINT uq_conversation_threads_thread UNIQUE (thread_id),
  KEY ix_conversation_threads_user_pinned_updated (user_id, deleted_at, pinned, updated_at, id),
  KEY ix_conversation_threads_user_status_updated (user_id, status, updated_at, id),
  KEY ix_conversation_threads_user_updated (user_id, deleted_at, updated_at, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='用户会话';

CREATE TABLE conversation_run_registrations (
  id BIGINT NOT NULL AUTO_INCREMENT COMMENT '运行记录 ID',
  conversation_thread_id BIGINT NOT NULL COMMENT '所属会话主键',
  run_id VARCHAR(128) NOT NULL COMMENT '主运行 ID',
  parent_run_id VARCHAR(128) COMMENT '分支或恢复的来源运行 ID',
  access_mode VARCHAR(32) NOT NULL DEFAULT 'full' COMMENT '文件审批模式：full、write_approval',
  model_id VARCHAR(64) NOT NULL COMMENT '主运行模型 ID',
  status VARCHAR(32) NOT NULL COMMENT '运行状态：preparing、starting、running、waiting、succeeded、failed、cancelled、abandoned',
  input_json JSON NOT NULL COMMENT '运行请求内容',
  service_bindings JSON NOT NULL COMMENT '搜索与图片生成服务 ID 及配置摘要',
  preparation_id VARCHAR(32) NOT NULL COMMENT '运行准备标识',
  terminal_outcome VARCHAR(32) COMMENT '运行终态结果',
  error_code VARCHAR(128) COMMENT '运行终态错误码',
  trace_generation VARCHAR(2048) COMMENT 'Trace 数据代次标识',
  trace_as_of_seq BIGINT COMMENT '会话摘要对应的 Trace 事件序号',
  trace_observed_at DATETIME(6) COMMENT 'UTC Trace 观测时间（微秒精度）',
  started_at DATETIME NOT NULL COMMENT 'UTC 请求注册时间',
  finished_at DATETIME COMMENT 'UTC 运行结束时间',
  created_at DATETIME NOT NULL COMMENT 'UTC 创建时间',
  updated_at DATETIME NOT NULL COMMENT 'UTC 最近状态更新时间',
  CONSTRAINT pk_conversation_run_registrations PRIMARY KEY (id),
  CONSTRAINT uq_conversation_run_registrations_thread_run UNIQUE (conversation_thread_id, run_id),
  KEY ix_conversation_run_registrations_thread_started (conversation_thread_id, started_at, id),
  KEY ix_conversation_run_registrations_thread_status (conversation_thread_id, status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='会话运行记录';

CREATE TABLE conversation_interrupt_claims (
  id BIGINT NOT NULL AUTO_INCREMENT COMMENT '认领主键',
  conversation_thread_id BIGINT NOT NULL COMMENT '所属会话主键',
  interrupt_id VARCHAR(255) NOT NULL COMMENT '交互中断 ID',
  source_run_id VARCHAR(128) NOT NULL COMMENT '交互来源运行 ID',
  claimed_run_id VARCHAR(128) NOT NULL COMMENT '交互恢复运行 ID',
  status VARCHAR(32) NOT NULL COMMENT '处理状态：claimed 已认领、resolved 已恢复、cancelled 已取消',
  resolution_id VARCHAR(128) COMMENT '交互恢复或取消凭据',
  created_at DATETIME NOT NULL COMMENT 'UTC 认领时间',
  resolved_at DATETIME COMMENT 'UTC 交互恢复时间',
  updated_at DATETIME NOT NULL COMMENT 'UTC 最近状态更新时间',
  CONSTRAINT pk_conversation_interrupt_claims PRIMARY KEY (id),
  CONSTRAINT uq_conversation_interrupt_claims_thread_interrupt UNIQUE (conversation_thread_id, interrupt_id),
  KEY ix_conversation_interrupt_claims_run_status (conversation_thread_id, claimed_run_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='会话交互恢复记录';

CREATE TABLE conversation_attachments (
  project_id VARCHAR(36) NOT NULL COMMENT '附件所属项目 ID',
	id VARCHAR(32) NOT NULL COMMENT '附件 ID',
	user_id INTEGER NOT NULL COMMENT '附件所属用户 ID',
	thread_id VARCHAR(128) COMMENT '所属会话 ID，草稿为空',
	message_id VARCHAR(128) COMMENT '首次使用或生成附件的消息 ID',
	name VARCHAR(255) NOT NULL COMMENT '文件名',
	mime_type VARCHAR(128) NOT NULL COMMENT '文件媒体类型，待确认上传为空',
	size_bytes BIGINT NOT NULL COMMENT '文件大小（字节），待确认上传为声明值',
	sha256 VARCHAR(64) NOT NULL COMMENT '文件内容 SHA-256 校验值',
	status VARCHAR(16) NOT NULL COMMENT '附件状态：uploading、processing、ready、deleting',
	source VARCHAR(16) NOT NULL COMMENT '附件来源：user 用户上传、tool 工具生成',
	created_at DATETIME NOT NULL COMMENT 'UTC 创建时间',
	PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='会话附件';
CREATE INDEX ix_conversation_attachments_cleanup ON conversation_attachments (thread_id, created_at);
CREATE INDEX ix_conversation_attachments_owner ON conversation_attachments (user_id, thread_id);

CREATE TABLE attachment_collections (
  project_id VARCHAR(36) NOT NULL COMMENT '集合所属项目 ID',
	id VARCHAR(36) NOT NULL COMMENT '附件集合 ID',
	user_id INTEGER NOT NULL COMMENT '所属用户 ID',
	purpose VARCHAR(16) NOT NULL COMMENT '集合用途：input 任务输入、execution 运行附件',
	task_id VARCHAR(36) COMMENT '关联自动化任务 ID',
	configuration JSON NOT NULL COMMENT '任务或运行输入快照',
	created_at DATETIME NOT NULL COMMENT 'UTC 创建时间',
	PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='自动化任务附件集合';

CREATE INDEX ix_attachment_collections_owner_task ON attachment_collections (user_id, task_id);

CREATE TABLE attachment_references (
	collection_id VARCHAR(36) NOT NULL COMMENT '附件集合 ID',
	attachment_id VARCHAR(32) NOT NULL COMMENT '附件 ID',
	PRIMARY KEY (collection_id, attachment_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='附件集合与文件关联';

CREATE INDEX ix_attachment_references_file ON attachment_references (attachment_id);

INSERT INTO users (username, display_name, avatar_url, password_hash, roles, disabled)
VALUES ('tinkerfin', 'TinkerFin', NULL, '$pbkdf2-sha256$600000$1ZFendL8broCk5OyW_zBQA$FHwGHqHzxz2n3-ksjSWYySw1tq6sE3X83sIsAGB1REI', '[]', FALSE);

CREATE TABLE skill_installations (
	project_id VARCHAR(36) NOT NULL DEFAULT '' COMMENT '所属项目 ID，空字符串表示个人库',
	id VARCHAR(36) NOT NULL COMMENT '技能安装 ID',
	user_id INTEGER NOT NULL COMMENT '安装所属用户 ID',
	name VARCHAR(64) COLLATE utf8mb4_bin NOT NULL COMMENT '技能名称',
	description VARCHAR(1024) NOT NULL COMMENT '技能描述',
	digest VARCHAR(64) NOT NULL COMMENT '技能目录 SHA-256 内容摘要',
	source_kind VARCHAR(16) NOT NULL COMMENT '安装来源：catalog、github、zip',
	source_id VARCHAR(64) COMMENT '技能来源 ID，个人导入为空',
	source_name VARCHAR(128) NOT NULL COMMENT '安装来源名称',
	external_id VARCHAR(256) COMMENT '来源中的发布者限定标识',
	source_revision VARCHAR(128) COMMENT '来源发布内容标识',
	source_url VARCHAR(2048) COMMENT '来源页面地址',
	author VARCHAR(256) COMMENT '技能作者',
	topics JSON NOT NULL COMMENT '技能分类列表',
	enabled BOOL NOT NULL COMMENT '是否启用技能',
	file_count INTEGER NOT NULL COMMENT '技能文件数',
	byte_size INTEGER NOT NULL COMMENT '技能文件总大小（字节）',
	created_at DATETIME NOT NULL COMMENT 'UTC 安装时间',
	updated_at DATETIME NOT NULL COMMENT 'UTC 安装状态更新时间',
	PRIMARY KEY (id),
	CONSTRAINT uq_skill_installations_owner_name UNIQUE (user_id, project_id, name)
)COMMENT='用户技能安装记录' ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE INDEX ix_skill_installations_owner_updated ON skill_installations (user_id, updated_at);

CREATE TABLE skill_import_drafts (
	confirmed_project_id VARCHAR(36) COMMENT '确认安装的项目 ID，个人库为空字符串，未确认为空值',
	id VARCHAR(36) NOT NULL COMMENT '导入预览 ID',
	user_id INTEGER NOT NULL COMMENT '导入所属用户 ID',
	source VARCHAR(16) NOT NULL COMMENT '导入来源：github、zip',
	source_url VARCHAR(2048) COMMENT 'GitHub 导入地址，ZIP 导入为空',
	candidates JSON NOT NULL COMMENT '技能预览条目及内容摘要',
	selected_digests JSON COMMENT '已确认的内容摘要列表，未确认为空',
	installation_ids JSON COMMENT '已确认的技能安装 ID 列表',
	created_at DATETIME NOT NULL COMMENT 'UTC 预览创建时间',
	PRIMARY KEY (id)
)COMMENT='技能导入预览与确认结果' ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE INDEX ix_skill_import_drafts_owner ON skill_import_drafts (user_id);

CREATE TABLE skill_run_snapshots (
	project_id VARCHAR(36) NOT NULL COMMENT '所属项目 ID',
	id VARCHAR(36) NOT NULL COMMENT '技能快照 ID',
	user_id INTEGER NOT NULL COMMENT '技能内容所属用户 ID',
	payload JSON NOT NULL COMMENT '技能安装 ID、名称、内容摘要及手动选择标记',
	created_at DATETIME NOT NULL COMMENT 'UTC 快照捕获时间',
	PRIMARY KEY (id)
)COMMENT='运行技能快照' ENGINE=InnoDB CHARSET=utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE INDEX ix_skill_run_snapshots_owner ON skill_run_snapshots (user_id);

CREATE TABLE skill_operation_receipts (
	user_id INTEGER NOT NULL COMMENT '操作所属用户 ID',
	request_id VARCHAR(128) NOT NULL COMMENT '技能操作请求 ID',
	fingerprint VARCHAR(64) NOT NULL COMMENT '规范化操作参数的 SHA-256 摘要',
	result JSON NOT NULL COMMENT '操作结果',
	created_at DATETIME NOT NULL COMMENT 'UTC 操作提交时间',
	PRIMARY KEY (user_id, request_id)
)COMMENT='技能操作回执' CHARSET=utf8mb4 COLLATE utf8mb4_0900_ai_ci ENGINE=InnoDB;
CREATE INDEX ix_skill_operation_receipts_owner_created ON skill_operation_receipts (user_id, created_at);

CREATE TABLE project_skill_settings (
  project_id VARCHAR(36) NOT NULL COMMENT '项目 ID',
  installation_id VARCHAR(36) NOT NULL COMMENT '技能安装 ID',
  user_id INTEGER NOT NULL COMMENT '所属用户 ID',
  enabled BOOL NOT NULL COMMENT '是否在项目中启用',
  PRIMARY KEY (project_id, installation_id),
  KEY ix_project_skill_settings_owner (user_id, project_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='项目个人技能启用配置';
