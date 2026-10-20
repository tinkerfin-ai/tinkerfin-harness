import { useState } from 'react'
import { Check, ChevronDown } from 'lucide-react'
import { Button, Dialog, ListboxPicker } from '../../components/ui'
import { useI18n } from '../../i18n'
import type { Conversation } from '../../types'
import type { Project } from './api'

export function MoveConversationDialog({ conversation, projects, trigger, pending, error, onConfirm, onClose }: {
  conversation: Conversation; projects: Project[]; trigger: HTMLElement; pending: boolean; error: string
  onConfirm: (projectId: string) => void; onClose: () => void
}) {
  const { t } = useI18n()
  const destinations = projects.filter(project => project.id !== conversation.projectId)
  const [target, setTarget] = useState(destinations[0]?.id ?? '')
  const [open, setOpen] = useState(false)
  return <Dialog open title={t('移动到项目')} restoreFocusTo={trigger} closeDisabled={pending} onClose={onClose}>
    <form className="projects-form" onSubmit={event => { event.preventDefault(); if (target) onConfirm(target) }}>
      <p>{t('移动后，会话会使用目标项目的技能、记忆与工作区')}</p>
      {destinations.length ? <div className="project-move-label"><span>{t('目标项目')}</span>
        <ListboxPicker value={target} options={destinations.map(project => project.id)} disabled={pending} onChange={setTarget} open={open} onOpenChange={setOpen}
          triggerLabel={t('目标项目')} listboxLabel={t('目标项目')} rootClassName="project-switcher project-move-picker"
          triggerClassName="project-switcher-trigger" listboxClassName="project-switcher-list" optionClassName="project-switcher-option"
          renderTrigger={value => <><span>{destinations.find(project => project.id === value)?.name}</span><ChevronDown size={16} aria-hidden="true" /></>}
          renderOption={(value, selected) => <><span>{destinations.find(project => project.id === value)?.name}</span>{selected && <Check size={16} aria-hidden="true" />}</>} />
      </div> : <p>{t('请先创建另一个项目')}</p>}
      {error && <p role="alert" className="project-move-error">{error}</p>}
      <div className="projects-form-actions"><Button type="button" disabled={pending} onClick={onClose}>{t('取消')}</Button><Button type="submit" variant="primary" loading={pending} disabled={!target}>{t('移动')}</Button></div>
    </form>
  </Dialog>
}
