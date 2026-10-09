import { act, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createRef, useRef, useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { Tooltip } from './Tooltip'
import { IconButton } from './IconButton'
import { Dialog } from './Dialog'
import { restoreFocus } from './focus'

describe('Tooltip', () => {
  it('鼠标显示统一提示，移入提示可继续阅读，移出后关闭', async () => {
    const interaction = userEvent.setup()
    render(<Tooltip content="模型设置"><button type="button">配置</button></Tooltip>)
    const button = screen.getByRole('button', { name: '配置' })
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
    await interaction.hover(button)
    const tip = screen.getByRole('tooltip')
    expect(button).toHaveAccessibleDescription('模型设置')
    fireEvent(button, new MouseEvent('pointerout', { bubbles: true, relatedTarget: tip }))
    fireEvent(tip, new MouseEvent('pointerover', { bubbles: true, relatedTarget: button }))
    expect(screen.getByRole('tooltip')).toBeVisible()
    fireEvent(tip, new MouseEvent('pointerout', { bubbles: true, relatedTarget: document.body }))
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })

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

  it('关闭弹框恢复入口焦点不显示提示，主动键盘进入仍可显示', async () => {
    function SearchHarness() {
      const [open, setOpen] = useState(false)
      const trigger = useRef<HTMLButtonElement>(null)
      const input = useRef<HTMLInputElement>(null)
      return <>
        <Tooltip content="搜索会话"><button ref={trigger} type="button" onClick={() => setOpen(true)}>搜索</button></Tooltip>
        <button type="button">下一项</button>
        <Dialog open={open} title="搜索会话" initialFocusRef={input} restoreFocusTo={trigger.current} onClose={() => setOpen(false)}>
          <input ref={input} aria-label="搜索对话" />
        </Dialog>
      </>
    }
    const interaction = userEvent.setup()
    render(<SearchHarness />)
    const trigger = screen.getByRole('button', { name: '搜索' })
    await interaction.click(trigger)
    expect(screen.getByRole('textbox', { name: '搜索对话' })).toHaveFocus()
    await interaction.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
    await interaction.tab()
    await interaction.tab({ shift: true })
    expect(trigger).toHaveFocus()
    expect(screen.getByRole('tooltip')).toHaveTextContent('搜索会话')
  })

  it('恢复入口不阻断后续悬停及主动键盘提示', async () => {
    const interaction = userEvent.setup()
    render(<>
      <Tooltip content="搜索会话"><button type="button">搜索</button></Tooltip>
      <button type="button">下一项</button>
    </>)
    const trigger = screen.getByRole('button', { name: '搜索' })
    act(() => restoreFocus(trigger, { preventScroll: true }))
    expect(trigger).toHaveFocus()
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
    await interaction.hover(trigger)
    expect(screen.getByRole('tooltip')).toHaveTextContent('搜索会话')
    await interaction.unhover(trigger)
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
    await interaction.tab()
    await interaction.tab({ shift: true })
    expect(screen.getByRole('tooltip')).toHaveTextContent('搜索会话')
    await interaction.keyboard('{Escape}')
    expect(trigger).toHaveFocus()
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

  it('只在文本实际溢出时提示完整内容', () => {
    const ref = createRef<HTMLSpanElement>()
    render(<Tooltip content="完整文件名" overflowOnly overflowRef={ref}><button ref={node => { ref.current = node }} type="button">完整文件名</button></Tooltip>)
    Object.defineProperties(ref.current!, { scrollWidth: { configurable: true, value: 10 }, clientWidth: { configurable: true, value: 10 } })
    fireEvent.pointerMove(ref.current!)
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
    Object.defineProperty(ref.current!, 'scrollWidth', { value: 20 })
    fireEvent.pointerMove(ref.current!)
    expect(screen.getByRole('tooltip')).toHaveTextContent('完整文件名')
  })

  it('多行截断保持宽度时仍能提供完整内容', () => {
    const ref = createRef<HTMLSpanElement>()
    render(<Tooltip content="完整多行文件名" overflowOnly overflowRef={ref}><span ref={ref}>文件名</span></Tooltip>)
    Object.defineProperties(ref.current!, { scrollWidth: { configurable: true, value: 100 }, clientWidth: { configurable: true, value: 100 },
      scrollHeight: { configurable: true, value: 40 }, clientHeight: { configurable: true, value: 20 } })
    fireEvent.pointerMove(ref.current!)
    expect(screen.getByRole('tooltip')).toHaveTextContent('完整多行文件名')
  })
})
