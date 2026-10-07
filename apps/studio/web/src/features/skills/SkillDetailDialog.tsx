import { ChevronDown, ExternalLink, X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Dialog, FeedbackState, IconButton } from '../../components/ui'
import { useI18n } from '../../i18n'
import { MarkdownContent } from '../conversation/components/MarkdownContent'
import { readInstalledSkill, readRemoteSkill } from './api'
import { SkillIcon } from './SkillCard'
import { skillError } from './useSkillData'
import type { SkillDetail, SkillSelection } from './model'

export function SkillDetailDialog({ projectId, selection, trigger, onClose }: { projectId: string | null; selection: SkillSelection; trigger: HTMLElement; onClose: () => void }) {
  const { t } = useI18n()
  const [detail, setDetail] = useState<SkillDetail | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [attempt, setAttempt] = useState(0)
  const closeRef = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    const controller = new AbortController()
    setError(null); setDetail(null)
    const request = selection.kind === 'installed' ? readInstalledSkill(projectId, selection.skill.id, controller.signal)
      : readRemoteSkill(selection.skill.source_id, selection.skill.id, selection.skill.revision, controller.signal).then(value => value.detail)
    void request.then(value => { if (!controller.signal.aborted) setDetail(value) }).catch(failure => { if (!controller.signal.aborted) setError(failure) })
    return () => controller.abort()
  }, [projectId, selection, attempt])
  return <Dialog open title={selection.skill.name} className="skills-detail-dialog" restoreFocusTo={trigger} initialFocusRef={closeRef} onClose={onClose}
    header={<header className="skills-detail-header"><SkillIcon name={selection.skill.name} /><div><h2>{selection.skill.name}</h2>
      <div className="skills-detail-byline">{selection.skill.author && <span>{selection.skill.author}</span>}{detail?.source_url && <a href={detail.source_url} target="_blank" rel="noopener noreferrer">{t('查看来源')}<ExternalLink size={12} aria-hidden="true" /></a>}</div>
    </div><IconButton ref={closeRef} type="button" label={t('关闭')} icon={<X size={19} />} onClick={onClose} /></header>}>
    <div className="skills-detail-scroll ui-scrollbar">
      {error ? <FeedbackState kind="error" title={skillError(error, t('技能详情加载失败'))} onRetry={() => setAttempt(value => value + 1)} />
        : !detail ? <FeedbackState kind="loading" title={t('正在读取技能说明')} /> : <>
          <p className="skills-detail-description">{detail.description}</p>
          {detail.topics.length > 0 && <div className="skills-detail-topics">{detail.topics.map(topic => <span key={topic}>{topic}</span>)}</div>}
          <details className="skills-description" open><summary><span className="skills-disclosure-label"><ChevronDown size={14} aria-hidden="true" /><span>{t('技能说明')}</span></span><span>SKILL.md</span></summary>
            <MarkdownContent className="skills-markdown" variant="compact" allowRemoteImages={false} content={detail.markdown.replace(/^---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)/, '')} />
          </details>
          {detail.files.length > 0 && <details className="skills-description"><summary><span className="skills-disclosure-label"><ChevronDown size={14} aria-hidden="true" /><span>{t('文件目录')}</span></span><span>{detail.files.length}</span></summary><ul className="skills-file-list">{detail.files.map(path => <li key={path}>{path}</li>)}</ul></details>}
        </>}
    </div>
  </Dialog>
}
