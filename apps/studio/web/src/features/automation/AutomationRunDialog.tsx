import { useEffect, useState } from 'react'
import { Download } from 'lucide-react'

import { Button, Dialog } from '../../components/ui'
import { isTranslationKey, useI18n } from '../../i18n'
import { MarkdownContent } from '../conversation/components/MarkdownContent'
import { messageText, type Attachment } from '../conversation/attachments/content'
import { useAttachmentDownload } from '../conversation/attachments/useAttachmentDownload'
import { fetchRunDetail, type RunDetail } from './api'
import { isActiveRun, presentRun, type AutomationRun } from './model'
import { RunStatus } from './AutomationHistory'

function ResultFile({ file }: { file: Attachment }) {
  const download = useAttachmentDownload(file)
  return <Button variant="ghost" leadingIcon={<Download size={16} />} loading={download.pending} onClick={() => void download.download()}>{file.name}</Button>
}

/** 只读取已授权结果；关闭后取消请求，运行中按服务端状态刷新 */
export function AutomationRunDialog({ run, trigger, onClose }: {
  run: AutomationRun | null
  trigger: HTMLElement | null
  onClose: () => void
}) {
  const { t } = useI18n()
  const [detail, setDetail] = useState<RunDetail | null>(null)
  const [failure, setFailure] = useState(false)
  const [revision, setRevision] = useState(0)
  const id = run?.id
  useEffect(() => {
    if (!id) return
    let controller: AbortController | null = null
    let closed = false
    let timer: ReturnType<typeof setTimeout> | undefined
    setDetail(null); setFailure(false)
    const read = async () => {
      if (closed || document.hidden) return
      controller?.abort()
      const request = new AbortController()
      controller = request
      try {
        const result = await fetchRunDetail(id, request.signal)
        if (closed || request.signal.aborted) return
        setDetail(result)
        if (isActiveRun(result.status) && !document.hidden) timer = setTimeout(() => void read(), 2000)
      } catch { if (!closed && !request.signal.aborted) setFailure(true) }
    }
    const visibility = () => { clearTimeout(timer); if (document.hidden) controller?.abort(); else void read() }
    void read()
    document.addEventListener('visibilitychange', visibility)
    return () => { closed = true; controller?.abort(); clearTimeout(timer); document.removeEventListener('visibilitychange', visibility) }
  }, [id, revision])
  const current = detail ? presentRun(detail) : run
  return <Dialog open={run !== null} title={t('运行结果')} className="automation-result-dialog" restoreFocusTo={trigger} onClose={onClose}>
    {current && <div className="automation-result-content">
      <h3>{current.name}</h3>
      <div className="automation-result-meta"><RunStatus run={current} /><time>{current.date} {current.time}</time>
        <span>{t(current.trigger === 'manual' ? '手动运行' : '定时运行')}</span></div>
      {failure ? <div role="alert">{t('运行结果加载失败')}<Button variant="text" onClick={() => setRevision(value => value + 1)}>{t('重试')}</Button></div>
        : !detail ? <p role="status">{t('正在加载运行结果')}</p> : <>
          {detail.error && <p role="status">{isTranslationKey(detail.error) ? t(detail.error) : detail.error}</p>}
          {!detail.resultAvailable && <p>{t('暂时没有可显示的结果')}</p>}
          <div className="automation-result-output">{detail.messages.filter(message => message.role === 'assistant').map(message => <MarkdownContent key={message.id} content={messageText(message.content)} />)}</div>
          {detail.outputFiles.map(file => <ResultFile key={file.id} file={file} />)}
        </>}
    </div>}
  </Dialog>
}
