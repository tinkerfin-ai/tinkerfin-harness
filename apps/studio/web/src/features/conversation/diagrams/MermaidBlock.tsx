import { useCallback, useId, useRef, useState } from 'react'
import { Expand } from 'lucide-react'
import { DiagramViewer } from './DiagramViewer'
import { Button, IconButton, ViewTabs } from '../../../components/ui'
import { CodeText } from '../../../components/ui/CodeText'
import { useI18n } from '../../../i18n'
import { CopyCodeButton } from '../components/CopyCodeButton'
import { SourceCodeBlock } from '../components/SourceCodeBlock'
import { useDiagram } from './useDiagram'
import './diagrams.css'

export function MermaidBlock({ source, isStreaming }: { source: string; isStreaming: boolean }) {
  const { t } = useI18n()
  const id = useId()
  const [expanded, setExpanded] = useState(false)
  const expandButton = useRef<HTMLButtonElement>(null)
  const closeViewer = useCallback(() => setExpanded(false), [])
  const [view, setView] = useState<'diagram' | 'source'>('diagram')
  const { diagram, error, retry } = useDiagram(source, isStreaming)
  if (error && error !== 'load' && error !== 'render') {
    return <SourceCodeBlock source={source} language="mermaid" label={t('Mermaid 图表')} />
  }
  const notice = isStreaming ? t('图表生成中…') : !source.trim() ? t('暂无图表内容')
    : error === 'load' ? t('图表组件加载失败，请重试')
      : error ? t('图表暂时无法显示，请重试') : t('正在渲染图表…')
  return <figure className="markdown-code-block mermaid-block" aria-label={t('Mermaid 图表')}>
    <figcaption className="markdown-code-block__head mermaid-block__head">
      <ViewTabs value={view} onChange={setView} label={t('图表视图')} density="compact" options={[
        { value: 'diagram', label: t('图表'), controls: `${id}-diagram` },
        { value: 'source', label: t('源码'), controls: `${id}-source` },
      ]} />
      <div className="mermaid-block__actions">
        {view === 'source'
          ? <CopyCodeButton source={source} diagram />
          : <IconButton ref={expandButton} type="button" variant="ghost" size="lg" label={t('放大图表')} tooltip={t('放大图表')} disabled={!diagram} icon={<Expand size={16} />} onClick={() => setExpanded(true)} />}
      </div>
    </figcaption>
    <div id={`${id}-diagram`} role="tabpanel" aria-label={t('图表')} hidden={view !== 'diagram'}>
      {diagram ? <div className="mermaid-block__canvas"><img src={diagram.url} alt={diagram.description || t('Mermaid 图表')} /></div>
        : <div className="mermaid-block__status" role="status">
          <span>{notice}</span>
          {error && <Button type="button" variant="text" onClick={retry}>{t('重试')}</Button>}
          {(isStreaming || error) && <Button type="button" variant="text" onClick={() => setView('source')}>{t('查看源码')}</Button>}
        </div>}
    </div>
    <div id={`${id}-source`} role="tabpanel" aria-label={t('源码')} hidden={view !== 'source'}>
      <pre role="region" aria-label={t('源码')} tabIndex={0}><CodeText language="mermaid" isStreaming={isStreaming}>{source}</CodeText></pre>
    </div>
    {expanded && diagram && <DiagramViewer diagram={diagram} source={source} onClose={closeViewer} returnFocus={expandButton.current} />}
  </figure>
}
