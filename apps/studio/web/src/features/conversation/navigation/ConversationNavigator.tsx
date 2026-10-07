import { List } from 'lucide-react'
import { memo, useEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'
import { Button, Dialog, IconButton, Tooltip } from '../../../components/ui'
import { restoreFocus } from '../../../components/ui/focus'
import { useI18n } from '../../../i18n'
import type { ConversationTurn } from './turns'
import './navigation.css'

interface Props {
  turns: ConversationTurn[]
  paneRef: RefObject<HTMLElement | null>
  open: boolean
  onOpenChange: (open: boolean) => void
  onNavigate: (messageId: string) => Promise<void>
}

/** 提供已加载对话的预览和阅读定位；弹窗关闭后才把焦点交给目标消息 */
export const ConversationNavigator = memo(function ConversationNavigator({
  turns, paneRef, open, onOpenChange, onNavigate,
}: Props) {
  const { t } = useI18n()
  const trigger = useRef<HTMLButtonElement>(null)
  const firstItem = useRef<HTMLButtonElement>(null)
  const rail = useRef<HTMLElement>(null)
  const [activeId, setActiveId] = useState<string | null>(null)
  const [previewMessageId, setPreviewMessageId] = useState<string | null>(null)
  const [pendingId, setPendingId] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const bandRef = useRef<HTMLDivElement>(null)
  const request = useRef(0)
  const previouslyOpen = useRef(open)
  const navigateRef = useRef(onNavigate)
  navigateRef.current = onNavigate
  const previewIndex = turns.findIndex(turn => turn.messageId === previewMessageId)
  const setPreviewIndex = (index: number | null) => setPreviewMessageId(index == null ? null : turns[index]?.messageId ?? null)

  useEffect(() => {
    const pane = paneRef.current
    if (!pane) return
    let frame = 0
    const measure = () => {
      frame = 0
      const bounds = pane.getBoundingClientRect()
      const line = bounds.top + Math.min(96, bounds.height * 0.2)
      const ids = new Set(turns.map(turn => turn.messageId))
      const nodes = Array.from(pane.querySelectorAll<HTMLElement>('.user-message'))
        .filter(node => ids.has(node.id))
      let current = nodes[0]?.id ?? null
      for (const node of nodes) {
        if (node.getBoundingClientRect().top <= line) current = node.id
      }
      if (pane.scrollHeight - pane.scrollTop - pane.clientHeight <= 2) current = nodes.at(-1)?.id ?? current
      setActiveId(current)
    }
    const schedule = () => {
      if (!frame) frame = requestAnimationFrame(measure)
    }
    const resize = new ResizeObserver(schedule)
    resize.observe(pane)
    if (pane.firstElementChild) resize.observe(pane.firstElementChild)
    const mutation = new MutationObserver(schedule)
    mutation.observe(pane, { childList: true, subtree: true })
    pane.addEventListener('scroll', schedule, { passive: true })
    schedule()
    return () => {
      cancelAnimationFrame(frame)
      resize.disconnect()
      mutation.disconnect()
      pane.removeEventListener('scroll', schedule)
    }
  }, [paneRef, turns])

  useEffect(() => {
    // 先解除工作区的模态隔离，再恢复取消操作的入口焦点
    if (previouslyOpen.current && !open && !pendingId) restoreFocus(trigger.current)
    previouslyOpen.current = open
  }, [open, pendingId])

  useEffect(() => {
    if (!pendingId || open) return
    const id = ++request.current
    setPendingId(null)
    setBusyId(pendingId)
    void navigateRef.current(pendingId).finally(() => {
      if (request.current === id) setBusyId(null)
    })
  }, [open, pendingId])

  useEffect(() => () => { request.current += 1 }, [])

  if (turns.length < 2) return null
  const select = (messageId: string) => {
    setPreviewIndex(null)
    onOpenChange(false)
    setPendingId(messageId)
  }
  const pointerIndex = (clientY: number) => {
    const bounds = rail.current?.getBoundingClientRect()
    if (!bounds) return 0
    return Math.round(Math.max(0, Math.min(1, (clientY - bounds.top - 6) / Math.max(1, bounds.height - 12))) * (turns.length - 1))
  }

  return (
    <>
      <div ref={bandRef} className="conversation-navigation" aria-busy={busyId != null}>
        <div className="conversation-navigation-mobile">
          <IconButton ref={trigger} label={t('对话目录')} tooltip={t('对话目录')} icon={<List size={18} />}
            aria-haspopup="dialog" aria-expanded={open} onClick={() => onOpenChange(true)} />
        </div>
        <nav ref={rail} className="conversation-navigation-rail" aria-label={t('对话目录')}
          style={{ height: `min(${(turns.length - 1) * 10 + 12}px, 100%)` }}
          onPointerMove={event => setPreviewIndex(pointerIndex(event.clientY))}
          onPointerLeave={() => setPreviewIndex(null)}>
          {turns.map((turn, index) => (
            <Tooltip key={turn.messageId} content={<><strong>{turn.prompt || t('未命名提问')}</strong>{turn.response && <p>{turn.response}</p>}</>} placement="left" className="ui-tooltip--prose"
              open={index === previewIndex} onOpenChange={visible => { if (visible) setPreviewIndex(index); else setPreviewMessageId(current => current === turn.messageId ? null : current) }}>
            <button type="button"
              className={`conversation-navigation-mark${turn.messageId === activeId ? ' is-current' : ''}${index === previewIndex ? ' is-preview' : ''}`}
              style={{ top: `calc(6px + (100% - 12px) * ${index / (turns.length - 1)})` }}
              aria-label={t('跳转到提问：{prompt}', { prompt: turn.prompt || t('未命名提问') })}
              aria-current={turn.messageId === activeId ? 'location' : undefined}
              tabIndex={turn.messageId === (activeId ?? turns[0].messageId) ? 0 : -1}
              onKeyDown={event => {
                if (event.key === 'Escape') { setPreviewIndex(null); return }
                const current = index
                const next = event.key === 'Home' ? 0 : event.key === 'End' ? turns.length - 1
                  : event.key === 'ArrowDown' ? Math.min(turns.length - 1, current + 1)
                    : event.key === 'ArrowUp' ? Math.max(0, current - 1) : null
                if (next == null) return
                event.preventDefault()
                rail.current?.querySelectorAll('button')[next]?.focus()
              }}
              onFocus={() => setPreviewIndex(index)} onBlur={() => setPreviewIndex(null)}
              onClick={event => select(turns[event.detail ? pointerIndex(event.clientY) : index].messageId)} /></Tooltip>
          ))}

        </nav>
      </div>
      <Dialog open={open} title={t('对话目录')} description={t('仅显示已加载的对话')}
        className="conversation-navigation-dialog" restoreFocusTo={trigger.current} initialFocusRef={firstItem}
        onClose={() => onOpenChange(false)}>
        <nav className="conversation-navigation-list ui-scrollbar" aria-label={t('已加载的提问')}>
          {turns.map((turn, index) => (
            <Button key={turn.messageId} ref={index === 0 ? firstItem : undefined} variant="ghost"
              className="conversation-navigation-item" selected={turn.messageId === activeId} aria-current={turn.messageId === activeId ? 'location' : undefined}
              onClick={() => select(turn.messageId)}>
              <strong>{turn.prompt || t('未命名提问')}</strong>
              {turn.response && <span>{turn.response}</span>}
            </Button>
          ))}
        </nav>
      </Dialog>
    </>
  )
})
