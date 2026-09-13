import { useContext, useLayoutEffect, useState } from 'react'

import type { Message } from '../../../types'
import { TextRevealProgressContext } from './textRevealProgress'

type LiveText = NonNullable<Message['liveText']>

const segmenter = new Intl.Segmenter(undefined, { granularity: 'grapheme' })

/** 每帧只显示一个完整字素，运行结束后继续显示已收到的正文 */
export function useTypewriterText(content: string, source: LiveText | undefined, complete: boolean) {
  const progress = useContext(TextRevealProgressContext)
  const key = source?.key
  const initialContent = source?.initialContent ?? content
  const [display, setDisplay] = useState(() => ({
    key,
    text: key ? progress?.get(key) ?? initialContent : content,
  }))
  if (source && !progress) throw new Error('实时正文需要工作台提供显示进度')
  const previous = display.key === key
    ? display.text
    : key ? progress?.get(key) ?? initialContent : content
  const text = !key ? content : content.startsWith(previous) ? previous : ''

  useLayoutEffect(() => {
    if (!key || !progress) return
    const saved = progress.get(key) ?? initialContent
    let offset = content.startsWith(saved) ? saved.length : 0
    const remaining = segmenter.segment(content.slice(offset))[Symbol.iterator]()
    let frame: number | undefined
    setDisplay({ key, text: content.slice(0, offset) })

    const reveal = () => {
      const next = remaining.next()
      if (next.done) return
      const end = offset + next.value.segment.length
      // 流中的最后一个字素可能尚未收全，例如组合音标或跨片段的表情
      if (!complete && end === content.length) return
      offset = end
      const value = content.slice(0, offset)
      progress.set(key, value)
      setDisplay({ key, text: value })
      if (offset < content.length) frame = window.requestAnimationFrame(reveal)
    }
    if (offset < content.length) frame = window.requestAnimationFrame(reveal)
    return () => {
      if (frame !== undefined) window.cancelAnimationFrame(frame)
    }
  }, [content, key, initialContent, progress, complete])

  return text
}
