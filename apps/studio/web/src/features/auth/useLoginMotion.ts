import { useGSAP } from '@gsap/react'
import gsap from 'gsap'
import type { RefObject } from 'react'

gsap.registerPlugin(useGSAP)

/** 登录表单整体滑入；用户开始输入或窗口变化时立即完成入场 */
export function useLoginMotion(rootRef: RefObject<HTMLElement | null>) {
  useGSAP(() => {
    const root = rootRef.current
    const panel = root?.querySelector<HTMLElement>('.auth-panel')
    if (!root || !panel) return
    const motion = gsap.matchMedia()
    motion.add('(prefers-reduced-motion: no-preference)', () => {
      gsap.set(panel, { y: innerHeight - panel.getBoundingClientRect().top + 24, willChange: 'transform', force3D: true })
      const entrance = gsap.to(panel, { y: 0, duration: 1.2, ease: 'power2.inOut', force3D: true, clearProps: 'transform,willChange' })
      const finish = () => { entrance.progress(1) }
      const syncVisibility = () => {
        if (document.hidden) finish()
      }
      panel.addEventListener('pointerdown', finish)
      panel.addEventListener('focusin', finish)
      window.addEventListener('resize', finish)
      document.addEventListener('visibilitychange', syncVisibility)
      syncVisibility()
      return () => {
        panel.removeEventListener('pointerdown', finish)
        panel.removeEventListener('focusin', finish)
        window.removeEventListener('resize', finish)
        document.removeEventListener('visibilitychange', syncVisibility)
      }
    })
    return () => motion.revert()
  }, { scope: rootRef })
}
