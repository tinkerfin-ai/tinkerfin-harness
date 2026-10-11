import { useEffect, useId, useRef, useState } from 'react'
import { ArrowRight, ArrowUpRight, CalendarDays, Check, Clock3, Copy, Files, MessageSquare, TriangleAlert } from 'lucide-react'

import { Button, Dialog, ErrorBoundary, FeedbackState, IconButton } from '../../components/ui'
import type { ToastHandler } from '../../components/ui/ToastViewport'
import { isTranslationKey, useI18n } from '../../i18n'
import { MarkdownContent } from '../conversation/components/MarkdownContent'
import { useCopyFeedback } from '../conversation/components/copyFeedback'
import { AttachmentList } from '../conversation/attachments/AttachmentList'
import { AttachmentReferenceContext } from '../conversation/attachments/context'
import { messageText, type Attachment } from '../conversation/attachments/content'
import { fetchRunDetail, type RunDetail } from './api'
import { watchResource } from '../../api/shared/watchResource'
import { isRunInProgress, presentRun, type AutomationRun, type AutomationRunRecord } from './model'
import { RunStatus } from './AutomationHistory'

export type OpenAutomationConversation = (run: Pick<AutomationRunRecord, 'id' | 'projectId'>, attachment?: Attachment, signal?: AbortSignal) => Promise<boolean>

/** 结果固定到原执行；刷新与会话入口请求随详情关闭而释放 */
export function AutomationRunDialog({ projectId, run, trigger, onToast, onClose, onOpenConversation }: {
  projectId: string
  run: AutomationRun
  trigger: HTMLElement | null
  onToast: ToastHandler
  onClose: () => void
  onOpenConversation: OpenAutomationConversation
}) {
  const { t } = useI18n()
  const [detail, setDetail] = useState<RunDetail | null>(null)
  const [failure, setFailure] = useState(false)
  const [revision, setRevision] = useState(0)
  const [opening, setOpening] = useState(false)
  const navigationRequest = useRef<AbortController | null>(null)
  const latestNotification = useRef({ onToast, t })
  latestNotification.current = { onToast, t }
  const answerId = useId(), filesId = useId()
  const filesHeading = useRef<HTMLHeadingElement>(null)
  const { state: copyState, copy } = useCopyFeedback()
  useEffect(() => {
    let observed: RunDetail | null = null
    const watch = watchResource({
      minRefreshMs: 2000,
      matches: change => (
        (change.topic === 'automation.execution.changed' && change.key === run.id)
        || (change.topic === 'studio.attachments.changed' && change.details.collection_id === run.id)
        || (change.topic === 'trace.changed' && (!observed || change.key === observed.threadId))
        || (change.topic === 'studio.conversation.changed' && change.key === observed?.threadId)
      ),
      read: signal => fetchRunDetail(projectId, run.id, signal),
      refreshWhile: isRunInProgress,
      update: result => { observed = result; setDetail(result); setFailure(false) },
      onError: () => {
        setFailure(true)
        latestNotification.current.onToast('error', latestNotification.current.t('运行结果加载失败'))
      },
    })
    return watch.close
  }, [projectId, run.id, revision])
  useEffect(() => () => navigationRequest.current?.abort(), [])
  const current = detail ? presentRun(detail) : run
  const active = isRunInProgress(current)
  const answers = detail?.messages.filter(message => message.role === 'assistant').map(message => ({ id: message.id, text: messageText(message.content) })).filter(message => message.text.trim()) ?? []
  const content = answers.map(message => message.text).join('\n\n')
  const copyLabel = t(copyState === 'copied' ? '已复制' : copyState === 'failed' ? '复制失败' : '复制完整回答')
  const openConversation = async (attachment?: Attachment) => {
    if (navigationRequest.current) return
    const controller = new AbortController()
    navigationRequest.current = controller
    setOpening(true)
    try {
      const opened = await onOpenConversation(run, attachment, controller.signal)
      if (opened && !controller.signal.aborted) onClose()
    } finally {
      if (navigationRequest.current === controller) navigationRequest.current = null
      if (!controller.signal.aborted) setOpening(false)
    }
  }
  return <Dialog open title={current.name} description={<div className="automation-result-meta">
    <RunStatus run={current} />
    <span><CalendarDays size={14} aria-hidden="true" /><time>{current.date} {current.time}</time></span>
    {current.finishedAt && current.startedAt && <span><Clock3 size={14} aria-hidden="true" />{t('用时 {seconds} 秒', { seconds: current.durationSeconds })}</span>}
    <span>{t(current.trigger === 'manual' ? '手动运行' : '定时运行')}</span>
  </div>} className="automation-result-dialog" restoreFocusTo={trigger} onClose={onClose}>
    <nav className="automation-result-jump" aria-label={t('详情分区')}>
      <span>{t('结果')}</span><Button type="button" variant="ghost" size="lg" trailingIcon={<ArrowRight size={16} />} onClick={() => {
        filesHeading.current?.scrollIntoView({ block: 'start' })
        filesHeading.current?.focus({ preventScroll: true })
      }}>{t('交付物')} {detail?.outputFiles.length ?? 0}</Button>
    </nav>
    <div className="automation-result-content ui-scrollbar">
      <div className="automation-result-answer ui-scrollbar" role="region" aria-labelledby={answerId} tabIndex={0}>
        <header className="automation-result-section-head"><h3 id={answerId}><MessageSquare size={17} aria-hidden="true" />{t('结果')}</h3>
          <IconButton type="button" variant="ghost" size="lg" label={copyLabel} tooltip={copyLabel} disabled={!content}
            icon={copyState === 'copied' ? <Check size={16} /> : copyState === 'failed' ? <TriangleAlert size={16} /> : <Copy size={16} />} onClick={() => void copy(content)} />
        </header>
        {failure && <FeedbackState kind="error" appearance="retry" title={t('运行结果加载失败')} retryLabel={t('重新加载')} onRetry={() => { setFailure(false); setRevision(value => value + 1) }} />}
        {!detail ? !failure && <FeedbackState kind="loading" title={t('正在加载运行结果')} /> : <>
          {detail.error && <p className="automation-result-error" role="status"><TriangleAlert size={18} aria-hidden="true" />{isTranslationKey(detail.error) ? t(detail.error) : detail.error}</p>}
          <ErrorBoundary resetKey={run.id} fallback={({ reset }) => <FeedbackState kind="error" title={t('运行结果加载失败')} onRetry={reset} />}>
            <div className="automation-result-output">{answers.map(message => <MarkdownContent key={message.id} content={message.text} isStreaming={active} />)}</div>
          </ErrorBoundary>
          {!content && !active && <p className="automation-result-empty">{t('暂时没有可显示的结果')}</p>}
          {active && <FeedbackState kind="loading" compact title={t('运行中，回答会继续更新')} />}
        </>}
      </div>
      <aside className="automation-result-files ui-scrollbar" role="region" aria-labelledby={filesId} tabIndex={0}>
        <header className="automation-result-section-head"><h3 id={filesId} ref={filesHeading} tabIndex={-1}><Files size={17} aria-hidden="true" />{t('交付物')}<span className="automation-result-count">{detail?.outputFiles.length ?? 0}</span></h3></header>
        {!detail ? !failure && <FeedbackState kind="loading" title={t('正在加载运行结果')} /> : detail.outputFiles.length ?
          <AttachmentReferenceContext.Provider value={attachment => { void openConversation(attachment) }}>
            <AttachmentList attachments={detail.outputFiles} />
          </AttachmentReferenceContext.Provider> : active ? <FeedbackState kind="loading" compact title={t('交付物正在准备中')} /> : <p className="automation-result-empty">{t('本次未生成交付物')}</p>}
      </aside>
    </div>
    <footer className="automation-result-footer">
      <Button type="button" variant="primary" size="lg" loading={opening} disabled={!detail || detail.conversation.state === 'not_created'}
        tooltip={detail?.conversation.state === 'not_created' ? t('关联会话尚未创建') : undefined}
        trailingIcon={<ArrowUpRight size={18} />} onClick={() => { void openConversation() }}>{t('查看关联会话')}</Button>
    </footer>
  </Dialog>
}
