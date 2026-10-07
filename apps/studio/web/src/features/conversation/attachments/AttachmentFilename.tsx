import { useRef } from 'react'
import { Button, Tooltip } from '../../../components/ui'

/** 文件名被截断时显示完整名称，沿用全局提示的键盘、悬浮和触控行为 */
export type AttachmentFilenameVariant = 'composer' | 'card'

export function AttachmentFilename({ name, variant = 'composer' }: { name: string; variant?: AttachmentFilenameVariant }) {
  const text = useRef<HTMLElement | null>(null)
  return <Tooltip content={name} placement="top" overflowOnly overflowRef={text} touchToggle>
    {variant === 'card' ? <Button type="button" variant="text" className="attachment-description-name"><strong ref={node => { text.current = node }} className="attachment-description__title">{name}</strong></Button>
      : <Button type="button" variant="text" className="composer-attachment-name"><span ref={node => { text.current = node }} className="composer-attachment-name-text">{name}</span></Button>}
  </Tooltip>
}
