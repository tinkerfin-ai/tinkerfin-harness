import { afterEach, describe, expect, it, vi } from 'vitest'
import { parseWidthPreference, persistWidthPreference, readWidthPreference, resolveConversationWidth } from './widthPreference'

afterEach(() => { localStorage.clear(); vi.restoreAllMocks() })

describe('会话宽度偏好', () => {
  it.each([[1200, 32, 768], [1600, 32, 920], [3840, 32, 920], [320, 12, 264]])('列宽 %i 使用安全范围内的自动宽度', (column, gutter, width) => {
    expect(resolveConversationWidth(column, gutter, null).width).toBe(width)
  })
  it('显示收窄不改变存储，回到宽屏恢复用户选择', () => {
    persistWidthPreference(1200)
    expect(resolveConversationWidth(900, 24, readWidthPreference()).width).toBe(820)
    expect(readWidthPreference()).toBe(1200)
    expect(resolveConversationWidth(1600, 32, readWidthPreference()).width).toBe(1200)
  })
  it.each([null, '', ' ', '-1', 'Infinity', 'NaN', '{}', '639'])('无有效偏好 %s 时采用自动模式', raw => {
    expect(parseWidthPreference(raw)).toBeNull()
  })
  it('浏览器禁用存储时仍可计算宽度', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new DOMException('denied') })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('denied') })
    expect(readWidthPreference()).toBeNull()
    expect(() => persistWidthPreference(900)).not.toThrow()
    expect(resolveConversationWidth(1200, 32, 900).width).toBe(900)
  })
})
