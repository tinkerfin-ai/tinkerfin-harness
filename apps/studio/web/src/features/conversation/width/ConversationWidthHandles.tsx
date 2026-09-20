import { useEffect, useRef, useState } from 'react'
import type { PointerEvent } from 'react'
import { useI18n } from '../../../i18n'
import type { ConversationWidthControl } from './useConversationWidth'
import { MIN_CONVERSATION_WIDTH } from './widthPreference'
import './width.css'

/** 两侧手柄同时调整消息与输入框宽度，取消手势时恢复已保存的选择 */
function WidthHandle({ side, control }: { side: 'left' | 'right'; control: ConversationWidthControl }) {
  const { t } = useI18n()
  const current = useRef(control)
  current.current = control
  const drag = useRef<{ id: number; origin: number; base: number; latest: number } | null>(null)
  const frame = useRef<number | null>(null)
  const [dragging, setDragging] = useState(false)
  const stopFrame = () => {
    if (frame.current !== null) cancelAnimationFrame(frame.current)
    frame.current = null
  }
  const proposed = (x: number) => {
    const gesture = drag.current
    return gesture ? gesture.base + (x - gesture.origin) * (side === 'right' ? 2 : -2) : current.current.width
  }
  const cancel = () => {
    if (!drag.current) return
    stopFrame()
    drag.current = null
    setDragging(false)
    current.current.cancel()
  }
  useEffect(() => () => {
    if (frame.current !== null) cancelAnimationFrame(frame.current)
    if (drag.current) current.current.cancel()
  }, [])

  const finish = (event: PointerEvent<HTMLDivElement>) => {
    if (drag.current?.id !== event.pointerId) return
    const value = proposed(event.clientX)
    const moved = event.clientX !== drag.current.origin
    stopFrame()
    drag.current = null
    setDragging(false)
    if (moved) current.current.commit(value)
    else current.current.cancel()
    event.currentTarget.releasePointerCapture(event.pointerId)
  }

  return <div
    role="separator"
    aria-label={t(side === 'left' ? '调整会话左侧宽度' : '调整会话右侧宽度')}
    aria-orientation="vertical"
    aria-controls="conversation-panel"
    className="conversation-width-handle"
    data-side={side}
    data-dragging={dragging || undefined}
    onPointerDown={event => {
      if (event.button !== 0 || drag.current) return
      event.preventDefault()
      drag.current = { id: event.pointerId, origin: event.clientX, latest: event.clientX, base: control.width }
      event.currentTarget.setPointerCapture(event.pointerId)
      setDragging(true)
    }}
    onPointerMove={event => {
      if (drag.current?.id !== event.pointerId) return
      drag.current.latest = event.clientX
      if (frame.current === null) frame.current = requestAnimationFrame(() => {
        frame.current = null
        if (drag.current) current.current.previewWidth(proposed(drag.current.latest))
      })
    }}
    onPointerUp={finish}
    onPointerCancel={cancel}
    onLostPointerCapture={cancel}
  >
    <span className="conversation-width-indicator" aria-hidden="true" />
  </div>
}

export function ConversationWidthHandles({ control }: { control: ConversationWidthControl }) {
  const { t } = useI18n()
  const followPointer = (event: PointerEvent<HTMLDivElement>) => {
    const bounds = event.currentTarget.getBoundingClientRect()
    event.currentTarget.style.setProperty('--width-pointer-y', `${event.clientY - bounds.top}px`)
  }
  if (control.max <= MIN_CONVERSATION_WIDTH) return null
  return <div className="conversation-width-handles" role="group" aria-label={t('调整会话宽度')}
    onPointerEnter={followPointer} onPointerMove={followPointer}>
    {(['left', 'right'] as const).map(side => <WidthHandle key={side} side={side} control={control} />)}
  </div>
}
