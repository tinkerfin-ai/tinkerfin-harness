import { beforeEach, describe, expect, it } from 'vitest'

import { LANGUAGE_STORAGE_KEY } from '../../i18n'
import { ConversationError, conversationErrorMessage, conversationRunError } from './errors'

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
  ['workspace_busy', '项目中还有任务在执行，请等待结束后再试'],
  ['workspace_file_conflict', '技能文件有本地修改，请先处理文件冲突后再试'],
])('运行错误 %s 保留可处理原因并隐藏诊断', (code, expected) => {
  window.localStorage.setItem(LANGUAGE_STORAGE_KEY, 'zh-CN')
  expect(conversationErrorMessage(conversationRunError(code, 'private diagnostics'), 'run_failed')).toBe(expected)
})
