import { describe, expect, it } from 'vitest'
import { formatDateRange } from './presentation'

describe('自动化日期范围', () => {
  it.each([
    ['2026-08-24', '2026-08-30', 'zh-CN', '8月24日 – 8月30日'],
    ['2026-08-31', '2026-09-06', 'zh-CN', '8月31日 – 9月6日'],
    ['2026-12-28', '2027-01-03', 'zh-CN', '2026年12月28日 – 2027年1月3日'],
    ['2026-08-24', '2026-08-30', 'en', 'Aug 24 – Aug 30'],
    ['2026-08-31', '2026-09-06', 'en', 'Aug 31 – Sep 6'],
    ['2026-12-28', '2027-01-03', 'en', 'Dec 28, 2026 – Jan 3, 2027'],
  ])('两端显示完整日期 %s 至 %s（%s）', (start, end, locale, expected) => {
    expect(formatDateRange(start, end, locale)).toBe(expected)
  })
})
