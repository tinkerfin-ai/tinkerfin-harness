import type { PointerEvent } from 'react'
import { useHorizontalResize } from '../../../components/ui/useHorizontalResize'
import { useI18n } from '../../../i18n'
import type { ConversationWidthControl } from './useConversationWidth'
import { MIN_CONVERSATION_WIDTH } from './widthPreference'
import './width.css'

/* eslint-disable jsx-a11y/no-noninteractive-tabindex -- 可调宽分隔线通过键盘与指针提供同等操作，插件将 separator 视作静态元素 */

/** 两侧手柄同时调整消息与输入框宽度，取消手势时恢复已保存的选择 */
function WidthHandle({ side, control }: { side: 'left' | 'right'; control: ConversationWidthControl }) {
  const { t } = useI18n()
  const { dragging, ...handlers } = useHorizontalResize({
    ...control, min: MIN_CONVERSATION_WIDTH, multiplier: side === 'right' ? 2 : -2,
  })

  return <div
    role="separator"
    tabIndex={0}
    aria-label={t(side === 'left' ? '调整会话左侧宽度' : '调整会话右侧宽度')}
    aria-orientation="vertical"
    aria-controls="conversation-panel"
    aria-valuemin={MIN_CONVERSATION_WIDTH}
    aria-valuemax={control.max}
    aria-valuenow={control.width}
    className="conversation-width-handle"
    data-side={side}
    data-dragging={dragging || undefined}
    {...handlers}
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
