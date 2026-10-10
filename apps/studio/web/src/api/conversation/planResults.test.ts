import { describe, expect, it } from 'vitest'
import { parsePlanResults } from './planResults'

const result = { interruptId: 'question', submissionRunId: 'resume', outcome: 'answered', reason: null,
  answers: { choice: { status: 'answered', answerType: 'single_choice', optionId: 'bottom', customAnswer: null } } }

describe('Plan 保存结果边界', () => {
  it('读取实际答案，收窄可空的选择字段', () => {
    expect(parsePlanResults([result])[0].answers?.choice).toEqual({ status: 'answered', answerType: 'single_choice', optionId: 'bottom' })
  })
  it.each([
    undefined,
    [result, result],
    [{ ...result, outcome: 'unknown' }],
    [{ ...result, answers: null }],
    [{ ...result, answers: { choice: { status: 'answered', answerType: 'single_choice', optionId: 'bottom', customAnswer: 'second answer' } } }],
    [{ ...result, answers: { choice: { status: 'answered', answerType: 'multiple_choice', optionIds: ['bottom', 'bottom'] } } }],
  ])('拒绝无法明确解释的结果：%j', input => {
    expect(() => parsePlanResults(input)).toThrow()
  })
})
