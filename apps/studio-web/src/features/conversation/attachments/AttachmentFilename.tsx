import { useRef } from 'react'
import { Button, Tooltip } from '../../../components/ui'

/** 文件名被截断时显示完整名称，沿用全局提示的键盘、悬浮和触控行为 */
export function AttachmentFilename({ name }: { name: string }) {
  const text = useRef<HTMLElement | null>(null)
  return <Tooltip content={name} placement="top" overflowOnly overflowRef={text} touchToggle>
    <Button type="button" variant="text" className="composer-attachment-name"><span ref={node => { text.current = node }} className="composer-attachment-name-text">{name}</span></Button>
  </Tooltip>
}
