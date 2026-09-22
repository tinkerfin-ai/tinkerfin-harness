import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ValidatedForm } from './ValidatedForm'

const animationMocks = vi.hoisted(() => ({
  add: vi.fn<(query: string, callback: () => void) => void>(),
  fromTo: vi.fn().mockReturnThis(),
  set: vi.fn().mockReturnThis(),
  registerPlugin: vi.fn(),
  revert: vi.fn(),
}))

vi.mock('gsap', () => ({
  default: {
    timeline: () => ({ fromTo: animationMocks.fromTo, set: animationMocks.set }),
    matchMedia: () => ({
      add: animationMocks.add,
      revert: animationMocks.revert,
    }),
    registerPlugin: animationMocks.registerPlugin,
  },
}))

vi.mock('@gsap/react', async () => {
  const React = await vi.importActual<typeof import('react')>('react')
  return {
    useGSAP: (callback: React.EffectCallback) => React.useLayoutEffect(callback),
  }
})

interface HarnessProps {
  errors?: Partial<Record<'first' | 'second', string>>
}

function Harness({
  errors = { first: '请输入第一项', second: '请输入第二项' },
}: HarnessProps) {
  const [validationAttempt, setValidationAttempt] = useState(0)

  return (
    <ValidatedForm
      aria-label="测试表单"
      errors={errors}
      validationAttempt={validationAttempt}
      onSubmit={(event) => {
        event.preventDefault()
        setValidationAttempt((current) => current + 1)
      }}
    >
      <label htmlFor="first">第一项</label>
      <input id="first" aria-invalid={Boolean(errors.first)} data-validation-feedback={errors.first ? 'invalid' : undefined} />
      <label htmlFor="second">第二项</label>
      <input id="second" aria-invalid={Boolean(errors.second)} data-validation-feedback={errors.second ? 'invalid' : undefined} />
      <button type="submit">提交</button>
    </ValidatedForm>
  )
}

describe('ValidatedForm', () => {
  beforeEach(() => {
    animationMocks.add.mockReset()
    animationMocks.add.mockImplementation((_query, callback) => callback())
    animationMocks.fromTo.mockClear()
    animationMocks.set.mockClear()
    animationMocks.revert.mockReset()
  })

  it('suppresses native validation', () => {
    render(<Harness />)

    expect(screen.getByRole('form', { name: '测试表单' })).toHaveAttribute('novalidate')
    expect(screen.getByLabelText('第一项')).toHaveAttribute('aria-invalid', 'true')
  })

  it('focuses the first invalid control and repeats feedback on every invalid submit', async () => {
    const user = userEvent.setup()
    render(<Harness />)

    await user.click(screen.getByRole('button', { name: '提交' }))

    expect(screen.getByLabelText('第一项')).toHaveFocus()
    expect(animationMocks.fromTo).toHaveBeenCalledTimes(1)

    await user.click(screen.getByRole('button', { name: '提交' }))
    expect(animationMocks.fromTo).toHaveBeenCalledTimes(2)
  })

  it('keeps focus feedback but skips shaking when reduced motion is requested', async () => {
    const user = userEvent.setup()
    animationMocks.add.mockImplementation(() => undefined)
    render(<Harness />)

    await user.click(screen.getByRole('button', { name: '提交' }))

    await waitFor(() => expect(screen.getByLabelText('第一项')).toHaveFocus())
    expect(animationMocks.fromTo).not.toHaveBeenCalled()
  })

  it('does not move focus or animate when the form has no errors', async () => {
    const user = userEvent.setup()
    render(<Harness errors={{}} />)
    const submitButton = screen.getByRole('button', { name: '提交' })

    await user.click(submitButton)

    expect(submitButton).toHaveFocus()
    expect(animationMocks.fromTo).not.toHaveBeenCalled()
  })
})
