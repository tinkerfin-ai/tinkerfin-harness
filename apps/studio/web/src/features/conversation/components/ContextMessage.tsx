import { BookOpen } from 'lucide-react'
import { useState } from 'react'
import type { Message } from '../../../types'
import { useI18n } from '../../../i18n'
import { ConversationDetailRow } from './ConversationDetailRow'
import { MarkdownContent } from './MarkdownContent'

/** 仅展示本次保存的上下文，展开不依赖技能当前是否仍安装 */
export function ContextMessage({ message }: { message: Message }) {
  const { t, locale } = useI18n()
  const [open, setOpen] = useState(false)
  const source = message.meta?.source
  const skills = source?.metadata?.skills
  const names = Array.isArray(skills) ? skills.flatMap(skill =>
    skill && typeof skill === 'object' && 'name' in skill && typeof skill.name === 'string' ? [skill.name] : []) : []
  const title = source?.name === 'skill-invocation' ? t('技能指令') : t('附加上下文')
  return <ConversationDetailRow id={message.id} title={title} summary={new Intl.ListFormat(locale, { style: 'short', type: 'conjunction' }).format(names) || (source?.name === 'skill-invocation' ? undefined : source?.name)}
    icon={<BookOpen size={14} strokeWidth={2} />} className="context-message" open={open} onOpenChange={setOpen}>
    <div className="context-message-content">
      {open && (message.meta?.contentOmitted
        ? <p>{t('该上下文正文未保留')}</p>
        : <MarkdownContent content={message.content} variant="compact" />)}
    </div>
  </ConversationDetailRow>
}
