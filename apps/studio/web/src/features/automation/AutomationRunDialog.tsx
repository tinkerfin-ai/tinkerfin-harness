import { useEffect, useRef, useState } from 'react'
import { Download } from 'lucide-react'

import { Button, Dialog, FeedbackState } from '../../components/ui'
import type { ToastHandler } from '../../components/ui/ToastViewport'
import { isTranslationKey, useI18n } from '../../i18n'
import { MarkdownContent } from '../conversation/components/MarkdownContent'
import { messageText, type Attachment } from '../conversation/attachments/content'
import { useAttachmentDownload } from '../conversation/attachments/useAttachmentDownload'
import { fetchRunDetail, type RunDetail } from './api'
import { watchResource } from '../../api/shared/watchResource'
import { isRunInProgress, presentRun, type AutomationRun } from './model'
import { RunStatus } from './AutomationHistory'

function ResultFile({ file }: { file: Attachment }) {
  const { t } = useI18n()
  const download = useAttachmentDownload(file)
  return <div className="automation-result-file" role="group" aria-label={file.name}>
    <Button variant="ghost" leadingIcon={<Download size={16} />} loading={download.pending} onClick={() => void download.download()}>{file.name}</Button>
    {download.failed && <FeedbackState kind="error" appearance="retry" title={t('下载失败，请重试')} retryLabel={t('重试下载')} onRetry={() => void download.download()} />}
  </div>
}

/** 只读取已授权结果；关闭后取消请求，接收运行、轨迹和附件变化后刷新 */
export function AutomationRunDialog({ projectId, run, trigger, onToast, onClose }: {
  projectId: string
  run: AutomationRun | null
  trigger: HTMLElement | null
  onToast: ToastHandler
  onClose: () => void
}) {
  const { t } = useI18n()
  const [detail, setDetail] = useState<RunDetail | null>(null)
  const [failure, setFailure] = useState(false)
  const [revision, setRevision] = useState(0)
  const loadedRunId = useRef<string | null>(null)
  const latestNotification = useRef({ onToast, t })
  latestNotification.current = { onToast, t }
  const id = run?.id
  useEffect(() => {
    if (!id) return
    if (loadedRunId.current !== id) {
      loadedRunId.current = id
      setDetail(null)
    }
    setFailure(false)
    let observed: RunDetail | null = null
    const watch = watchResource({
      minRefreshMs: 2000,
      matches: change => (
        (change.topic === 'automation.execution.changed' && change.key === id)
        || (change.topic === 'studio.attachments.changed' && change.details.collection_id === id)
        || (change.topic === 'trace.changed' && (!observed || change.key === observed.threadId))
      ),
      read: signal => fetchRunDetail(projectId, id, signal),
      refreshWhile: isRunInProgress,
      update: result => { observed = result; setDetail(result); setFailure(false) },
      onError: () => {
        setFailure(true)
        latestNotification.current.onToast('error', latestNotification.current.t('运行结果加载失败'))
      },
    })
    return watch.close
  }, [projectId, id, revision])
  const current = detail ? presentRun(detail) : run
  return <Dialog open={run !== null} title={t('运行结果')} className="automation-result-dialog" restoreFocusTo={trigger} onClose={onClose}>
    {current && <div className="automation-result-content" role="region" aria-label={t('运行结果')} tabIndex={0}>
      <h3>{current.name}</h3>
      <div className="automation-result-meta"><RunStatus run={current} /><time>{current.date} {current.time}</time>
        <span>{t(current.trigger === 'manual' ? '手动运行' : '定时运行')}</span></div>
      {failure && <div className="automation-result-failure"><FeedbackState kind="error" appearance="retry" title={t('运行结果加载失败')} retryLabel={t('重新加载')} onRetry={() => setRevision(value => value + 1)} /></div>}
      {!detail ? !failure && <FeedbackState kind="loading" title={t('正在加载运行结果')} /> : <>
          {detail.error && <p role="status">{isTranslationKey(detail.error) ? t(detail.error) : detail.error}</p>}
          {!detail.resultAvailable && <p>{t('暂时没有可显示的结果')}</p>}
          <div className="automation-result-output">{detail.messages.filter(message => message.role === 'assistant').map(message => <MarkdownContent key={message.id} content={messageText(message.content)} />)}</div>
          {detail.outputFiles.map(file => <ResultFile key={file.id} file={file} />)}
        </>}
    </div>}
  </Dialog>
}
