import { beforeEach, describe, expect, it } from 'vitest'

import { LANGUAGE_STORAGE_KEY } from '../../i18n'
import { ConversationError, conversationErrorMessage } from './errors'
import { ApiError } from '../shared/http'

describe('conversation error boundary', () => {
  beforeEach(() => {
    window.localStorage.clear()
  })

  it('只把稳定错误码映射到当前语言，不暴露内部诊断', () => {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, 'en')
    const error = new ConversationError(
      'state_patch_invalid',
      'JSON Patch remove 目标不存在：/private/token',
    )

    const message = conversationErrorMessage(error, 'run_request_failed')

    expect(message).toBe('The conversation state could not be updated. Try again')
    expect(message).not.toContain('private/token')
    expect(error.diagnostic).toContain('private/token')
  })

  it('未知异常使用调用边界声明的恢复文案', () => {
    expect(conversationErrorMessage(
      new Error('底层连接信息'),
      'stream_recovery_failed',
    )).toBe('会话 Trace 恢复失败，请重试')
  })
})


it.each([
  ['zh-CN', '所选技能正文合计超过 512 KiB，请减少所选技能后重试'],
  ['en', 'Selected skill instructions exceed 512 KiB. Select fewer skills and try again'],
])('技能正文超限在 %s 下给出操作提示，不展示服务端诊断', (locale, expected) => {
  window.localStorage.setItem(LANGUAGE_STORAGE_KEY, locale)
  const error = new ApiError('private diagnostics', { code: 1_001_008_014, status: 422 })
  expect(conversationErrorMessage(error, 'run_request_failed')).toBe(expected)
})
