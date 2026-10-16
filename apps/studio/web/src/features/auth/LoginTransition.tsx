import { useGSAP } from '@gsap/react'
import gsap from 'gsap'
import { useRef } from 'react'

import { BrandMark, MOTION_DURATION_SECONDS } from '../../components/ui'
import { useI18n } from '../../i18n'

gsap.registerPlugin(useGSAP)

export function LoginTransition({ onComplete }: { onComplete: () => void }) {
  const { t } = useI18n()
  const rootRef = useRef<HTMLElement>(null)
  const onCompleteRef = useRef(onComplete)
  onCompleteRef.current = onComplete

  useGSAP(() => {
    const root = rootRef.current
    const mark = root?.querySelector<HTMLElement>('.login-transition__mark')
    if (!root || !mark) return

    const timeline = gsap.timeline({
      onComplete: () => onCompleteRef.current(),
    })
    timeline
      .fromTo(root, { autoAlpha: 0 }, { autoAlpha: 1, duration: MOTION_DURATION_SECONDS.fast, ease: 'power1.out' })
      .fromTo(mark, { scale: 0.84, rotate: -8 }, { scale: 1, rotate: 0, duration: MOTION_DURATION_SECONDS.normal, ease: 'power2.out' }, 0)
      .to(root, { autoAlpha: 0, duration: MOTION_DURATION_SECONDS.fast, ease: 'power1.in' }, MOTION_DURATION_SECONDS.normal)

    return () => timeline.kill()
  }, { scope: rootRef })

  return (
    <main id="main-content" ref={rootRef} className="login-transition" aria-label={t('正在进入工作区')}>
      <span className="login-transition__mark" aria-hidden="true"><BrandMark size={28} /></span>
    </main>
  )
}
