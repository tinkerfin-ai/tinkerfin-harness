import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useStreamingContentScroll } from './useStreamingContentScroll'

function Field({ identity = 'field', value = '内容', running = true, visible = true, node = 'first' }: {
  identity?: string; value?: string; running?: boolean; visible?: boolean; node?: string
}) {
  const { viewportRef, contentRef } = useStreamingContentScroll({ identity, value, running, visible })
  return (
    <div key={node} ref={viewportRef} role="region" aria-label="流式字段" tabIndex={0}>
      <div ref={contentRef}>{value}<button type="button">复制</button></div>
    </div>
  )
}

let height: number
let viewportHeight: number
let frames: Map<number, FrameRequestCallback>
let observers: Map<ResizeObserver, { callback: ResizeObserverCallback; targets: Set<Element> }>

function flushFrames() {
  act(() => {
    const pending = [...frames.values()]
    frames.clear()
    pending.forEach((callback) => callback(0))
  })
}

function resize(element: Element) {
  act(() => {
    observers.forEach(({ callback, targets }, observer) => {
      if (targets.has(element)) callback([], observer)
    })
  })
}

function scroll(viewport: HTMLElement, top: number) {
  viewport.scrollTop = top
  fireEvent.scroll(viewport)
}

beforeEach(() => {
  height = 500
  viewportHeight = 150
  frames = new Map()
  observers = new Map()
  let nextFrame = 0
  vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockImplementation(() => height)
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockImplementation(() => viewportHeight)
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
    const id = ++nextFrame
    frames.set(id, callback)
    return id
  })
  vi.stubGlobal('cancelAnimationFrame', (id: number) => frames.delete(id))
  vi.stubGlobal('ResizeObserver', class implements ResizeObserver {
    constructor(callback: ResizeObserverCallback) { observers.set(this, { callback, targets: new Set() }) }
    observe(target: Element) { observers.get(this)!.targets.add(target) }
    unobserve(target: Element) { observers.get(this)!.targets.delete(target) }
    disconnect() { observers.delete(this) }
  })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('流式字段的阅读位置', () => {
  it.each(['wheel', 'touch', 'keyboard', 'scrollbar'] as const)('%s 上翻暂停跟随，回到底部两像素内恢复', (input) => {
    const { rerender } = render(<Field />)
    const viewport = screen.getByRole('region')
    expect(viewport.scrollTop).toBe(350)
    if (input === 'wheel') fireEvent.wheel(viewport, { deltaY: -80 })
    if (input === 'keyboard') fireEvent.keyDown(viewport, { key: 'PageUp' })
    if (input === 'touch') {
      fireEvent.touchStart(viewport, { touches: [{ clientX: 0, clientY: 100 }] })
      fireEvent.touchMove(viewport, { touches: [{ clientX: 0, clientY: 180 }] })
      fireEvent.touchEnd(viewport)
    }
    scroll(viewport, 270)
    height = 800
    rerender(<Field value="追加内容" />)
    expect(viewport.scrollTop).toBe(270)
    scroll(viewport, 647)
    height = 900
    rerender(<Field value="仍在阅读旧内容" />)
    expect(viewport.scrollTop).toBe(647)
    scroll(viewport, 748)
    height = 1000
    rerender(<Field value="回到底部后的内容" />)
    expect(viewport.scrollTop).toBe(850)
  })

  it('滚轮上翻后即使内容先于滚动事件提交，也不抢回底部', () => {
    const { rerender } = render(<Field />)
    const viewport = screen.getByRole('region')
    fireEvent.wheel(viewport, { deltaY: -80 })
    height = 800
    rerender(<Field value="同一轮追加" />)
    expect(viewport.scrollTop).toBe(350)
    scroll(viewport, 270)
    rerender(<Field value="继续追加" />)
    expect(viewport.scrollTop).toBe(270)
  })

  it.each([false, true])('原生滚动条上移与内容增长同批发生时保留阅读位置，滚动事件先到达 %s', (scrollFirst) => {
    const { rerender } = render(<Field />)
    const viewport = screen.getByRole('region')
    viewport.scrollTop = 270
    height = 800
    if (scrollFirst) fireEvent.scroll(viewport)
    rerender(<Field value="滚动事件派发前追加" />)
    fireEvent.scroll(viewport)
    expect(viewport.scrollTop).toBe(270)
    height = 900
    rerender(<Field value="继续追加" />)
    expect(viewport.scrollTop).toBe(270)
  })

  it.each([false, true])('用户回到底部与内容增长同批发生时恢复跟随，滚动事件先到达 %s', (scrollFirst) => {
    const { rerender } = render(<Field />)
    const viewport = screen.getByRole('region')
    scroll(viewport, 120)
    viewport.scrollTop = 348
    height = 800
    if (scrollFirst) fireEvent.scroll(viewport)
    rerender(<Field value="到达底部时追加" />)
    fireEvent.scroll(viewport)
    expect(viewport.scrollTop).toBe(650)
  })

  it('悬停、点击、横向滚动和子控件按键不暂停跟随', () => {
    const { rerender } = render(<Field />)
    const viewport = screen.getByRole('region')
    viewport.scrollLeft = 20
    fireEvent.pointerEnter(viewport)
    fireEvent.pointerDown(viewport)
    fireEvent.pointerUp(viewport)
    fireEvent.wheel(viewport, { deltaX: -100, deltaY: -1 })
    fireEvent.keyDown(screen.getByRole('button'), { key: 'Home' })
    height = 800
    rerender(<Field value="继续输出" />)
    fireEvent.scroll(viewport)
    height = 900
    rerender(<Field value="再次输出" />)
    expect(viewport.scrollTop).toBe(750)
    expect(viewport.scrollLeft).toBe(20)
  })

  it.each([false, true])('折叠期间不定位，展开时保留暂停状态 %s 和阅读位置', (paused) => {
    const { rerender } = render(<Field />)
    const viewport = screen.getByRole('region')
    if (paused) scroll(viewport, 120)
    rerender(<Field visible={false} />)
    scroll(viewport, 0)
    height = 800
    rerender(<Field visible={false} value="折叠期间完成" running={false} />)
    expect(viewport.scrollTop).toBe(0)
    rerender(<Field value="折叠期间完成" running={false} />)
    expect(viewport.scrollTop).toBe(paused ? 120 : 650)
  })

  it('历史内容以及未展开就完成的字段首次打开均从顶部阅读', () => {
    const { rerender } = render(<Field visible={false} />)
    rerender(<Field value="完整内容" visible={false} running={false} />)
    rerender(<Field value="完整内容" running={false} />)
    const viewport = screen.getByRole('region')
    expect(viewport.scrollTop).toBe(0)
    scroll(viewport, 120)
    height = 800
    resize(viewport)
    flushFrames()
    expect(viewport.scrollTop).toBe(120)
  })

  it.each([false, true])('最后内容和停止运行同时提交时尊重暂停状态 %s', (paused) => {
    const { rerender } = render(<Field />)
    const viewport = screen.getByRole('region')
    if (paused) scroll(viewport, 120)
    height = 800
    rerender(<Field value="最后内容" running={false} />)
    expect(viewport.scrollTop).toBe(paused ? 120 : 650)
    height = 900
    resize(screen.getByText('最后内容'))
    flushFrames()
    expect(viewport.scrollTop).toBe(paused ? 120 : 750)
  })

  it('内容自然高度和视口尺寸变化均保持底部，布局收缩不恢复已暂停的跟随', () => {
    render(<Field />)
    const viewport = screen.getByRole('region')
    height = 800
    resize(screen.getByText('内容'))
    flushFrames()
    expect(viewport.scrollTop).toBe(650)
    viewportHeight = 100
    resize(viewport)
    flushFrames()
    expect(viewport.scrollTop).toBe(700)
    height = 250
    viewport.scrollTop = 150
    resize(screen.getByText('内容'))
    flushFrames()
    height = 800
    resize(screen.getByText('内容'))
    flushFrames()
    expect(viewport.scrollTop).toBe(700)
    scroll(viewport, 200)
    height = 250
    scroll(viewport, 150)
    resize(viewport)
    flushFrames()
    height = 800
    resize(screen.getByText('内容'))
    flushFrames()
    expect(viewport.scrollTop).toBe(150)
  })

  it('字段身份改变后不沿用其他内容的跟随或阅读位置', () => {
    const { rerender } = render(<Field />)
    const viewport = screen.getByRole('region')
    scroll(viewport, 120)
    resize(viewport)
    rerender(<Field identity="history" running={false} />)
    flushFrames()
    expect(viewport.scrollTop).toBe(0)
    rerender(<Field identity="another-live-field" />)
    expect(viewport.scrollTop).toBe(350)
  })

  it('节点替换保留阅读意图，并在卸载时移除观察器、监听和待执行定位', () => {
    const { rerender, unmount } = render(<Field />)
    const oldViewport = screen.getByRole('region')
    scroll(oldViewport, 120)
    resize(oldViewport)
    rerender(<Field node="replacement" />)
    const viewport = screen.getByRole('region')
    expect(viewport.scrollTop).toBe(120)
    scroll(oldViewport, 350)
    height = 800
    rerender(<Field node="replacement" value="新内容" />)
    flushFrames()
    expect(viewport.scrollTop).toBe(120)
    resize(viewport)
    unmount()
    expect(frames.size).toBe(0)
    expect(observers.size).toBe(0)
  })
})
