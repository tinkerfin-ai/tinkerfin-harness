import { useHorizontalResize } from './useHorizontalResize'
import { MIN_DRAWER_WIDTH, type DrawerLayout } from './useDrawerLayout'

/* eslint-disable jsx-a11y/no-noninteractive-element-interactions, jsx-a11y/no-noninteractive-tabindex -- 可调宽分隔线是支持键盘的 ARIA 控件，插件仍将其视为非交互元素 */

/** 左侧分隔线支持指针调宽与方向键调宽，Home/End 选择当前可用的两端 */
export function DrawerResizeHandle({ control, label, controls }: {
  control: DrawerLayout
  label: string
  controls: string
}) {
  const { dragging, ...pointerHandlers } = useHorizontalResize({ ...control, multiplier: -1 })
  return <div
    role="separator"
    tabIndex={0}
    aria-label={label}
    aria-orientation="vertical"
    aria-controls={controls}
    aria-valuemin={MIN_DRAWER_WIDTH}
    aria-valuemax={control.max}
    aria-valuenow={control.width}
    className="ui-drawer-resize-handle"
    data-dragging={dragging || undefined}
    {...pointerHandlers}
    onKeyDown={event => {
      const width = event.key === 'Home' ? MIN_DRAWER_WIDTH
        : event.key === 'End' ? control.max
        : event.key === 'ArrowLeft' ? control.width + 16
        : event.key === 'ArrowRight' ? control.width - 16
        : undefined
      if (width === undefined) return
      event.preventDefault()
      control.commit(width)
    }}
  />
}
