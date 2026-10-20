import { useEffect, type RefObject } from 'react'

/** 卡片滚动区接近末尾时预取下一页，失败期间暂停自动触发 */
export function useSkillPagination(
  root: RefObject<HTMLDivElement | null>,
  sentinel: RefObject<HTMLDivElement | null>,
  enabled: boolean,
  load: () => Promise<void>,
) {
  useEffect(() => {
    if (!enabled || !root.current || !sentinel.current) return
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) void load()
    }, { root: root.current, rootMargin: '0px 0px 320px', threshold: 0.01 })
    observer.observe(sentinel.current)
    return () => observer.disconnect()
  }, [enabled, load, root, sentinel])
}
