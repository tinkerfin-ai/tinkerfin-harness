import { useGSAP } from '@gsap/react'
import gsap from 'gsap'
import type { RefObject } from 'react'

gsap.registerPlugin(useGSAP)

/** 登录表单整体滑入；背景只移动已成像的本地光带图层 */
export function useLoginMotion(rootRef: RefObject<HTMLElement | null>) {
  useGSAP(() => {
    const root = rootRef.current
    const panel = root?.querySelector<HTMLElement>('.auth-panel')
    if (!root || !panel) return
    const motion = gsap.matchMedia()
    motion.add('(prefers-reduced-motion: no-preference)', () => {
      gsap.set(panel, { y: innerHeight - panel.getBoundingClientRect().top + 24, willChange: 'transform', force3D: true })
      const entrance = gsap.to(panel, { y: 0, duration: 1.2, ease: 'power2.inOut', force3D: true, clearProps: 'transform,willChange' })
      const common = { ease: 'sine.inOut', repeat: -1, yoyo: true, force3D: true }
      const background = [
        gsap.to(root.querySelector('.auth-ribbon--blue'), { ...common, xPercent: 3.125, yPercent: 4.375, rotation: 2, scale: 1.035, duration: 7 }),
        gsap.to(root.querySelector('.auth-ribbon--violet'), { ...common, xPercent: -3.125, yPercent: -3.75, rotation: -2, scale: 1.04, duration: 9 }),
        gsap.to(root.querySelector('.auth-ambient-mist'), { ...common, xPercent: 7, yPercent: -7, scale: 1.12, duration: 8 }),
      ]
      const finish = () => { entrance.progress(1) }
      const syncVisibility = () => {
        background.forEach((tween) => document.hidden ? tween.pause() : tween.resume())
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
