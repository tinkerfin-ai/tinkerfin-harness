import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useRef, useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { ConversationNavigator } from './ConversationNavigator'

const turns = [
  { messageId: 'question-a', prompt: '第一个问题', response: '第一段回答' },
  { messageId: 'question-b', prompt: '第二个问题', response: '' },
]

function Harness({ navigate }: { navigate: (id: string) => Promise<void> }) {
  const paneRef = useRef<HTMLElement>(null)
  const [open, setOpen] = useState(false)
  return <>
    <section ref={paneRef}><article id="question-a" className="user-message" /><article id="question-b" className="user-message" /></section>
    <ConversationNavigator turns={turns} paneRef={paneRef} open={open} onOpenChange={setOpen} onNavigate={navigate} />
  </>
}

describe('对话快速导航交互', () => {
  it('目录取消恢复入口焦点，选择后关闭并只定位一次', async () => {
    const navigate = vi.fn(async () => {})
    render(<Harness navigate={navigate} />)
    const trigger = screen.getByRole('button', { name: '对话目录' })
    fireEvent.click(trigger)
    const dialog = screen.getByRole('dialog', { name: '对话目录' })
    expect(within(dialog).getByRole('button', { name: '第一个问题 第一段回答' })).toHaveFocus()
    fireEvent.keyDown(dialog, { key: 'Escape' })
    expect(trigger).toHaveFocus()
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
    expect(navigate).not.toHaveBeenCalled()
    fireEvent.click(trigger)
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '第二个问题' }))
    await waitFor(() => expect(navigate).toHaveBeenCalledExactlyOnceWith('question-b'))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('方向键浏览预览，Escape 收起，空回答不伪造摘要', () => {
    render(<Harness navigate={async () => {}} />)
    const first = screen.getByRole('button', { name: '跳转到提问：第一个问题' })
    fireEvent.focus(first)
    expect(screen.getByRole('tooltip')).toHaveTextContent('第一段回答')
    fireEvent.keyDown(first, { key: 'ArrowDown' })
    const second = screen.getByRole('button', { name: '跳转到提问：第二个问题' })
    expect(second).toHaveFocus()
    expect(screen.getByRole('tooltip')).toHaveTextContent('第二个问题')
    expect(screen.getByRole('tooltip')).not.toHaveTextContent('第一段回答')
    fireEvent.keyDown(second, { key: 'Escape' })
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })
})
