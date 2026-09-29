import { SseError } from '../shared/sse'
import { ApiError } from '../shared/http'
import { translateCurrent, type TranslationKey } from '../../i18n'

const conversationErrorMessageKeys = {
  stream_limit_exceeded: '实时输出数据量过大，请重试',
  stream_data_invalid: '实时输出数据无法解析，请重试',
  stream_event_invalid: '实时输出事件格式不正确，请重试',
  stream_body_missing: '实时输出连接没有返回数据，请重试',
  stream_disconnected: '实时输出连接意外中断，请重试',
  stream_recovery_failed: '会话 Trace 恢复失败，请重试',
  stream_sequence_invalid: '实时事件顺序异常，请重试',
  state_patch_invalid: '会话状态更新失败，请重试',
  run_request_failed: '对话请求失败，请重试',
  run_failed: '对话运行失败',
  run_initialization_failed: '任务初始化失败，请重试',
  resume_failed: '继续任务失败，请重新提交',
  approval_stale: '当前审批已更新，请重新检查',
  approval_incomplete: '请先处理所有待审批项',
  plan_stale: '当前 Plan 请求已更新，请重新检查',
  plan_already_submitted: '当前 Plan 请求已经提交',
  plan_required_answers_missing: '请回答所有必填的 Plan 澄清问题',
  plan_option_required: '该问题必须选择一个选项',
  plan_answer_invalid: 'Plan 澄清答案无效，请检查后重试',
  plan_action_required: '请选择 Plan 处理方式',
  plan_submit_failed: 'Plan 请求无法提交',
} as const satisfies Record<string, TranslationKey>

export type ConversationErrorCode = keyof typeof conversationErrorMessageKeys

/**
 * 表示 Studio 能稳定恢复或提示的会话错误
 *
 * `diagnostic` 只用于测试、日志和定位，界面必须通过 `conversationErrorMessage`
 * 取得面向用户的本地化文案，避免把协议载荷或内部异常直接暴露给用户
 */
export class ConversationError extends Error {
  readonly code: ConversationErrorCode
  readonly diagnostic: unknown

  constructor(code: ConversationErrorCode, diagnostic?: unknown) {
    super(code)
    this.name = 'ConversationError'
    this.code = code
    this.diagnostic = diagnostic
  }
}

/** 将稳定错误码翻译为用户可操作的提示，不拼接内部诊断 */
export const conversationErrorMessage = (
  error: unknown,
  fallback: ConversationErrorCode,
) => translateCurrent(
  error instanceof ApiError && error.code === 1_001_008_014
    ? '所选技能正文合计超过 512 KiB，请减少所选技能后重试'
    : conversationErrorMessageKeys[
    error instanceof ConversationError || error instanceof SseError ? error.code : fallback
  ],
)

export const hasConversationErrorCode = (
  error: unknown,
  code: ConversationErrorCode,
) => (error instanceof ConversationError || error instanceof SseError) && error.code === code
