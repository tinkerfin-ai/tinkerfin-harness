import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createRef } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { Tooltip } from './Tooltip'
import { IconButton } from './IconButton'

describe('Tooltip', () => {

  it('保留引用与调用方事件，键盘聚焦显示提示，Escape 关闭但不移动焦点', async () => {
    const interaction = userEvent.setup(), onFocus = vi.fn(), ref = createRef<HTMLButtonElement>()
    render(<Tooltip content="设置说明"><button ref={ref} type="button" onFocus={onFocus}>设置</button></Tooltip>)
    await interaction.tab()
    expect(ref.current).toHaveFocus()
    expect(onFocus).toHaveBeenCalledOnce()
    expect(screen.getByRole('tooltip')).toHaveTextContent('设置说明')
    await interaction.keyboard('{Escape}')
    expect(ref.current).toHaveFocus()
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })

  it('图标按钮显式配置提示，禁用时仍可说明操作但不能点击', () => {
    const onClick = vi.fn()
    render(<IconButton type="button" label="设为默认对话" tooltip="设为默认对话" icon={<span />} disabled onClick={onClick} />)
    const button = screen.getByRole('button', { name: '设为默认对话' })
    fireEvent.pointerMove(button)
    expect(screen.getByRole('tooltip')).toHaveTextContent('设为默认对话')
    expect(button).not.toHaveAttribute('title')
    fireEvent.click(button)
    expect(onClick).not.toHaveBeenCalled()
  })

  it('关闭提示能力时不增加不存在的描述关系', () => {
    render(<Tooltip content="搜索说明" enabled={false}><button type="button" aria-describedby="reason">搜索</button></Tooltip>)
    const button = screen.getByRole('button', { name: '搜索' })
    fireEvent.pointerMove(button)
    expect(button).toHaveAttribute('aria-describedby', 'reason')
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })
})
