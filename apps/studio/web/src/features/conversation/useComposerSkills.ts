import { useMemo } from 'react'
import { useInstalledSkills } from '../skills/useInstalledSkills'
import type { ComposerSkill } from './composerSuggestions'
import type { ComposerSkillReference } from './composerDraft'

/** 目录刷新只更新可用性，已插入的引用名称和原文保持不变 */
export function useComposerSkills(projectId: string, active: boolean, references: readonly ComposerSkillReference[]) {
  const library = useInstalledSkills(projectId, active)
  const skills = useMemo<ComposerSkill[]>(() => library.items.filter(item => item.enabled && !item.overridden).map(({ id, name, description }) => ({ id, name, description })), [library.items])
  const selected = useMemo<ComposerSkill[]>(() => references.map(({ skill }) => {
    const current = skills.find(item => item.id === skill.id && item.name === skill.name)
    return current ? { ...skill, description: current.description } : { ...skill, unavailable: library.status === 'ready' || skill.unavailable }
  }), [references, skills, library.status])
  return { skills, selected, status: library.status, retry: library.refresh }
}
