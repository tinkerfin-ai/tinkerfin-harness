import { useGSAP } from '@gsap/react'
import gsap from 'gsap'
import { useRef } from 'react'
import type { FormHTMLAttributes } from 'react'

import { MOTION_DURATION_SECONDS } from './motion'

gsap.registerPlugin(useGSAP)

interface ValidatedFormProps<FieldName extends string>
  extends Omit<FormHTMLAttributes<HTMLFormElement>, 'noValidate'> {
  errors: Partial<Record<FieldName, string>>
  validationAttempt: number
}
/** 统一字段错误反馈与焦点定位，不显示浏览器原生校验气泡 */
export function ValidatedForm<FieldName extends string>({
  errors,
  validationAttempt,
  children,
  ...formProps
}: ValidatedFormProps<FieldName>) {
  const formRef = useRef<HTMLFormElement>(null)

  useGSAP(() => {
    if (validationAttempt === 0 || !Object.values(errors).some(Boolean)) return

    const firstInvalidControl = formRef.current?.querySelector<HTMLElement>(
      '[aria-invalid="true"]:not(:disabled)',
    )
    const invalidFeedbackTargets = formRef.current?.querySelectorAll<HTMLElement>(
      '[data-validation-feedback="invalid"]',
    )
    firstInvalidControl?.focus()
    if (!invalidFeedbackTargets?.length) return

    const media = gsap.matchMedia()
    media.add('(prefers-reduced-motion: no-preference)', () => {
      // 连续换向并逐次收幅，让错误反馈呈现短促抖动，最后自然回到原位
      gsap.timeline().fromTo(invalidFeedbackTargets, { x: 0 }, {
        keyframes: {
          x: [0, -6, 6, -4, 4, -2, 2, 0],
          easeEach: 'none',
        },
        duration: MOTION_DURATION_SECONDS.slow,
        ease: 'none',
        overwrite: 'auto',
      }).set(invalidFeedbackTargets, { clearProps: 'transform' })
    })
    return () => media.revert()
  }, {
    dependencies: [validationAttempt],
    scope: formRef,
    revertOnUpdate: true,
  })

  return <form {...formProps} ref={formRef} noValidate>{children}</form>
}
