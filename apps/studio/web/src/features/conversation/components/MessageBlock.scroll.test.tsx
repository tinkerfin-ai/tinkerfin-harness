import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Message } from '../../../types'
import { MessageBlock, ToolCallBatch } from './MessageBlock'

const tool: Message = {
  id: 'scroll-tool', role: 'tool', content: '', createdAt: '2026-09-20T00:00:00Z',
  meta: { toolName: 'read_file', params: '第一段参数', status: 'running' },
}
const subagent: Message = {
  ...tool, id: 'scroll-subagent', role: 'subagent',
  meta: { agentName: 'researcher', result: '第一段输出', status: 'running' },
}

function geometry(viewport: HTMLElement, height: number) {
  Object.defineProperties(viewport, {
    clientHeight: { configurable: true, value: 150 },
    scrollHeight: { configurable: true, value: height },
  })
}

function expand(title: string, open = true) {
  const disclosure = screen.getByText(title).closest('details')!
  disclosure.open = open
  fireEvent(disclosure, new Event('toggle'))
  return disclosure
}

beforeEach(() => {
  vi.stubGlobal('requestAnimationFrame', vi.fn(() => 1))
  vi.stubGlobal('cancelAnimationFrame', vi.fn())
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('工具卡片流式阅读', () => {
  it('运行中的输入参数展开及追加后显示底部', () => {
    const { rerender } = render(<MessageBlock message={tool} />)
    const input = screen.getByText('输入').parentElement!
    geometry(input, 500)
    expand('Read')
    expect(input.scrollTop).toBe(350)

    geometry(input, 800)
    rerender(<MessageBlock message={{ ...tool, meta: { ...tool.meta, params: '第一段参数\n第二段参数' } }} />)
    expect(input.scrollTop).toBe(650)
  })

  it('子 Agent 的输出展开及追加后显示底部', () => {
    const { rerender } = render(<MessageBlock message={subagent} />)
    const output = screen.getByRole('region', { name: '输出', hidden: true })
    geometry(output, 500)
    expand('SubAgent')
    expect(output.scrollTop).toBe(350)

    geometry(output, 800)
    rerender(<MessageBlock message={{ ...subagent, meta: { ...subagent.meta, result: '第一段输出\n\n第二段输出' } }} />)
    expect(output.scrollTop).toBe(650)
  })

  it('同批次新增第二个工具时保留首个工具的展开和阅读位置', () => {
    const { rerender } = render(<ToolCallBatch messages={[tool]} />)
    expand('Read')
    const input = screen.getByText('输入').parentElement!
    geometry(input, 500)
    act(() => { input.scrollTop = 120 })
    fireEvent.scroll(input)

    rerender(<ToolCallBatch messages={[tool, { ...tool, id: 'second-tool', meta: { toolName: 'ls', status: 'running' } }]} />)
    expect(screen.getByText('Read').closest('details')).toHaveAttribute('open')
    expect(screen.getAllByText('输入')[0].parentElement!.scrollTop).toBe(120)
  })

  it('嵌套工具在父卡片折叠期间保留自己的阅读位置', () => {
    const { rerender } = render(<MessageBlock message={subagent} childTools={[tool]} />)
    expand('SubAgent')
    const input = screen.getByText('输入').parentElement!
    geometry(input, 500)
    expand('Read')
    expect(input.scrollTop).toBe(350)
    fireEvent.wheel(input, { deltaY: -80 })
    input.scrollTop = 120
    fireEvent.scroll(input)
    expand('SubAgent', false)
    input.scrollTop = 0
    fireEvent.scroll(input)
    geometry(input, 800)
    rerender(<MessageBlock message={subagent} childTools={[{ ...tool, meta: { ...tool.meta, params: '追加参数' } }]} />)
    expect(input.scrollTop).toBe(0)
    expand('SubAgent')
    expect(input.scrollTop).toBe(120)
  })

  it.each(['completed', 'failed', 'cancelled', 'paused'] as const)('工具 %s 时最后参数仍跟随，完整结果从顶部阅读', (status) => {
    const { rerender } = render(<MessageBlock message={tool} />)
    const input = screen.getByText('输入').parentElement!
    const output = screen.getByText('输出').parentElement!
    geometry(input, 500)
    geometry(output, 800)
    expand('Read')
    geometry(input, 800)
    rerender(<MessageBlock message={{ ...tool, meta: { ...tool.meta, params: '最终参数', result: '完整结果', status } }} />)
    expect(input.scrollTop).toBe(650)
    expect(output.scrollTop).toBe(0)
  })

  it('子 Agent 在展开时尚无正文，最终正文到达后仍跟随', () => {
    const { rerender } = render(<MessageBlock message={{ ...subagent, meta: { ...subagent.meta, result: '' } }} />)
    expand('SubAgent')
    vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(800)
    vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(180)
    rerender(<MessageBlock message={{ ...subagent, meta: { ...subagent.meta, result: '最终正文', status: 'completed' } }} />)
    expect(screen.getByRole('region', { name: '输出' }).scrollTop).toBe(620)
  })
})
