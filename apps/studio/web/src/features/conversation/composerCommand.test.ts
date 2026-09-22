import { describe, expect, it } from 'vitest'

import { parseComposerSubmission } from './composerCommand'

describe('parseComposerSubmission', () => {
  it.each([
    ['/plan', null],
    ['  /plan \n\t', null],
    ['/plan 制定发布方案', { kind: 'plan-message', content: '制定发布方案' }],
    ['/plan   多空格正文  ', { kind: 'plan-message', content: '多空格正文' }],
    ['/plan off', { kind: 'plan-off-unsupported' }],
    ['/planner', { kind: 'message', content: '/planner' }],
    ['/PLAN', { kind: 'message', content: '/PLAN' }],
    ['普通消息', { kind: 'message', content: '普通消息' }],
  ] as const)('parses %s without leaking command syntax', (input, expected) => {
    expect(parseComposerSubmission(input)).toEqual(expected)
  })
})

it('裸 compact 执行操作，附加文字返回使用提示', () => {
  expect(parseComposerSubmission('  /compact  ')).toEqual({ kind: 'compact' })
  expect(parseComposerSubmission('/compact 保留')).toEqual({ kind: 'compact-arguments-unsupported' })
})
