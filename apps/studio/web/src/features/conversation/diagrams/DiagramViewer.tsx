import { ArrowLeft, Minus, Plus } from 'lucide-react'
import { useContext, useEffect, useRef } from 'react'
import type { RefObject } from 'react'
import { Button, Dialog, IconButton } from '../../../components/ui'
import { DialogDetailContext } from '../../../components/ui/DialogContext'
import { DialogDetail } from '../../../components/ui/DialogDetail'
import { useMediaViewport } from '../../../components/ui/useMediaViewport'
import { useI18n } from '../../../i18n'
import type { RenderedDiagram } from './renderDiagram'

interface DiagramViewerProps {
  diagram: RenderedDiagram
  source: string
  onClose: () => void
  returnFocus: HTMLElement | null
}

function DiagramCanvas({ diagram, source, onClose, backRef }: Omit<DiagramViewerProps, 'returnFocus'> & { backRef: RefObject<HTMLButtonElement | null> }) {
  const { t } = useI18n()
  const viewport = useMediaViewport(source)
  const { setMedia } = viewport
  useEffect(() => setMedia({ width: diagram.width, height: diagram.height }), [diagram.width, diagram.height, setMedia, source])
  return <div className="diagram-viewer">
    <header className="diagram-viewer__head">
      <Button ref={backRef} type="button" variant="ghost" size="lg" aria-label={t('返回原内容')} onClick={onClose} leadingIcon={<ArrowLeft size={18} />}>{t('返回')}</Button>
      <h2>{t('图表')}</h2>
      <span aria-hidden="true" />
    </header>
    {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- 可滚动画布支持键盘缩放和平移，保留区域语义以便阅读图表 */}
    <div ref={viewport.stage} className="diagram-viewer__stage ui-media-viewport" role="region" aria-label={t('图表画布')} tabIndex={0}
      data-pannable={viewport.canPan}
      onPointerDown={viewport.pointerDown} onPointerMove={viewport.pointerMove} onPointerUp={viewport.pointerEnd}
      onPointerCancel={viewport.pointerEnd} onLostPointerCapture={viewport.pointerEnd}
      onKeyDown={event => {
        const actions: Record<string, () => void> = {
          '+': viewport.zoomIn, '=': viewport.zoomIn, '-': viewport.zoomOut,
          '0': () => viewport.changeZoom('fit'), '1': () => viewport.changeZoom(1),
          ArrowLeft: () => viewport.pan(-64, 0), ArrowRight: () => viewport.pan(64, 0),
          ArrowUp: () => viewport.pan(0, -64), ArrowDown: () => viewport.pan(0, 64),
        }
        if (!event.ctrlKey && !event.metaKey && actions[event.key]) {
          event.preventDefault()
          actions[event.key]()
        }
      }}>
      <div className="ui-media-viewport__content" style={viewport.frameStyle}>
        <img src={diagram.url} alt={diagram.description || t('Mermaid 图表')} style={viewport.mediaStyle} draggable={false} />
      </div>
    </div>
    <footer className="diagram-viewer__tools" aria-label={t('图表缩放')}>
      <IconButton type="button" label={t('缩小')} tooltip={t('缩小')} icon={<Minus size={18} />} onClick={viewport.zoomOut} disabled={!viewport.canZoomOut} />
      <Button type="button" variant="ghost" size="lg" aria-label={t('重置为 100%')} title={t('重置为 100%')} onClick={() => viewport.changeZoom(1)}>{Math.round(viewport.scale * 100)}%</Button>
      <IconButton type="button" label={t('放大')} tooltip={t('放大')} icon={<Plus size={18} />} onClick={viewport.zoomIn} disabled={!viewport.canZoomIn} />
      <Button type="button" variant="ghost" size="lg" onClick={() => viewport.changeZoom('fit')}>{t('适应画布')}</Button>
    </footer>
  </div>
}

export function DiagramViewer(props: DiagramViewerProps) {
  const { t } = useI18n()
  const host = useContext(DialogDetailContext)
  const backRef = useRef<HTMLButtonElement>(null)
  const canvas = <DiagramCanvas {...props} backRef={backRef} />
  return host
    ? <DialogDetail title={t('图表')} onClose={props.onClose}>{canvas}</DialogDetail>
    : <Dialog open fullScreen header={null} title={t('图表')} onClose={props.onClose} initialFocusRef={backRef} restoreFocusTo={props.returnFocus}>{canvas}</Dialog>
}
