import { useGSAP } from '@gsap/react'
import gsap from 'gsap'
import { Flip } from 'gsap/Flip'
import { useRef, type RefObject } from 'react'

import { MOTION_DURATION_SECONDS } from '../../components/ui/motion'

gsap.registerPlugin(useGSAP, Flip)

const LAYOUT_TARGET_SELECTOR = '[data-workspace-layout-target]'

/**
 * 在侧栏或右侧抽屉布局提交后恢复空间连续性
 *
 * 业务状态仍由 React 和 CSS 决定；Flip 只读取提交前后的几何并通过 transform、
 * opacity 播放过渡，因此快速反向或卸载不会留下第二份布局状态
 */
export function useWorkspaceLayoutAnimation({
  shellRef,
  layoutKey,
}: {
  shellRef: RefObject<HTMLDivElement | null>
  layoutKey: string
}) {
  const previousState = useRef<Flip.FlipState | null>(null)
  const animation = useRef<gsap.core.Timeline | null>(null)

  useGSAP(() => {
    const shell = shellRef.current
    const targets = shell
      ? [...shell.querySelectorAll<HTMLElement>(LAYOUT_TARGET_SELECTOR)]
      : []
    const state = previousState.current
    previousState.current = null

    animation.current?.kill()
    animation.current = null
    if (
      state
      && targets.length > 0
      && !window.matchMedia('(prefers-reduced-motion: reduce)').matches
    ) {
      animation.current = Flip.from(state, {
        targets,
        duration: MOTION_DURATION_SECONDS.slow,
        ease: 'power2.inOut',
        simple: true,
        scale: true,
        prune: true,
        toggleClass: 'is-layout-flipping',
      })
    }

    return () => {
      const liveTargets = shellRef.current
        ? [...shellRef.current.querySelectorAll<HTMLElement>(LAYOUT_TARGET_SELECTOR)]
        : []
      animation.current?.kill()
      animation.current = null
      Flip.killFlipsOf(liveTargets, false)
      previousState.current = liveTargets.length > 0
        ? Flip.getState(liveTargets, { props: 'opacity', simple: true })
        : null
    }
  }, {
    dependencies: [layoutKey],
    scope: shellRef,
  })
}
