import { LoaderCircle } from 'lucide-react'
import { useLayoutEffect, useRef } from 'react'

/** 所有运行提示共用文档时间起点，后加入和重新显示的会话直接接上当前相位 */
export function ConversationRunIndicator() {
  const icon = useRef<SVGSVGElement>(null)
  const synchronize = () => {
    for (const animation of icon.current?.getAnimations() ?? []) animation.startTime = 0
  }
  useLayoutEffect(synchronize, [])
  return <LoaderCircle ref={icon} className="conversation-loading" onAnimationStart={synchronize} />
}
