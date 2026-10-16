import { useId, type KeyboardEventHandler } from 'react'
import { useI18n } from '../../i18n'
import type { Project } from '../projects/api'

/** 安装和导入共用位置选择，已安装的位置不可重复提交 */
export function SkillDestinationPicker({ project, value, onChange, onKeyDown, disabled = false, installedIn = [] }: {
  project: Pick<Project, 'id' | 'name'>
  value: string | null
  onChange: (projectId: string | null) => void
  onKeyDown?: KeyboardEventHandler<HTMLInputElement>
  disabled?: boolean
  installedIn?: readonly (string | null)[]
}) {
  const { t } = useI18n()
  const name = useId()
  const options = [
    { value: project.id, label: t('当前项目'), description: project.name },
    { value: null, label: t('个人共用'), description: t('在我的所有项目中可用') },
  ]
  return <fieldset className="skills-destination" disabled={disabled}>
    <legend>{t('安装位置')}</legend>
    {options.map(option => <label key={option.value ?? 'personal'} htmlFor={`${name}-${option.value ?? 'personal'}`} className="skills-destination-option">
      <input id={`${name}-${option.value ?? 'personal'}`} type="radio" name={name} value={option.value ?? ''} checked={value === option.value} disabled={installedIn.includes(option.value)} onChange={() => onChange(option.value)} onKeyDown={onKeyDown} />
      <span>{option.label}{installedIn.includes(option.value) && <span className="skills-destination-installed">{t('已安装')}</span>}<small>{option.description}</small></span>
    </label>)}
  </fieldset>
}
