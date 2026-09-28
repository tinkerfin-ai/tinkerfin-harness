import { useCallback, useMemo, useRef, useState } from 'react'
import { useInstalledSkills } from '../skills/useInstalledSkills'
import type { ComposerSkill } from './composerSuggestions'

export interface SubmittedSkills { revision: number; skills: readonly ComposerSkill[] }

/** 本轮技能选择只在发送受理后清空；失败和迟到响应不会改写新的草稿 */
export function useComposerSkills(active: boolean) {
  const library = useInstalledSkills(active)
  const skills = useMemo<ComposerSkill[]>(() => library.items.filter(item => item.enabled).map(({ id, name, description }) => ({ id, name, description })), [library.items])
  const [selected, setSelected] = useState<ComposerSkill[]>([])
  const visibleSelected = useMemo<ComposerSkill[]>(() => selected.map(item => skills.find(skill => skill.id === item.id) ?? { ...item, unavailable: true }), [selected, skills])
  const revision = useRef(0)
  const choose = useCallback((id: string) => {
    const skill = skills.find(item => item.id === id)
    if (!skill) return
    revision.current += 1
    setSelected(current => current.some(item => item.id === id) || current.length >= 8 ? current : [...current, skill])
  }, [skills])
  const remove = useCallback((id: string) => {
    revision.current += 1
    setSelected(current => current.filter(item => item.id !== id))
  }, [])
  const clear = useCallback(() => { revision.current += 1; setSelected([]) }, [])
  const acknowledge = useCallback((submitted: SubmittedSkills) => {
    if (revision.current !== submitted.revision) return
    revision.current += 1; setSelected([])
  }, [])
  return { skills, selected: visibleSelected, status: library.status, choose, remove, clear, acknowledge,
    capture: (): SubmittedSkills => ({ revision: revision.current, skills: visibleSelected }),
    retry: library.refresh,
  }
}
