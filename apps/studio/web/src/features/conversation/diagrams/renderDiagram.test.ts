import { beforeEach, describe, expect, it, vi } from 'vitest'
import { diagramImage, renderDiagram, validateDiagramSource } from './renderDiagram'

const engine = vi.hoisted(() => ({ initialize: vi.fn(), detectType: vi.fn(), parse: vi.fn(), render: vi.fn() }))
vi.mock('mermaid', () => ({ default: engine }))
const svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 120"><title>测试流程</title><rect width="100" height="50"/></svg>'

beforeEach(() => {
  engine.initialize.mockReset()
  engine.detectType.mockReset().mockReturnValue('flowchart-v2')
  engine.parse.mockReset().mockResolvedValue({ diagramType: 'flowchart-v2' })
  engine.render.mockReset().mockResolvedValue({ svg })
})

describe('图表渲染边界', () => {
  it.each(['%%{init: {"securityLevel":"loose"}}%%\nflowchart LR\nA-->B', '---\nconfig:\n  theme: dark\n---\nflowchart LR\nA-->B', 'flowchart LR\nA@{img: "https://example.com/a.png"}', 'flowchart LR\nstyle A fill:url(https://example.com/a)', 'flowchart LR\nstyle A fill:u\\72l(test)'])('拒绝修改宿主配置和外部资源', source => {
    expect(() => validateDiagramSource(source)).toThrow('unsafe')
  })

  it('超限源码由当前图表失败，不替换成引擎错误图', () => {
    expect(() => validateDiagramSource('a'.repeat(50_001))).toThrow('too-large')
  })

  it('将 SVG 净化为无交互图片，并保留可访问描述及固有尺寸', () => {
    const image = diagramImage(svg.replace('</svg>', '<script>alert(1)</script><foreignObject><div>html</div></foreignObject><image href="https://example.com/image"/></svg>'))
    const result = decodeURIComponent(image.url.split(',')[1])
    expect(result).not.toMatch(/<script|foreignObject|<image|onload/)
    expect(image).toMatchObject({ width: 300, height: 120, description: '测试流程' })
  })

  it('先结束当前渲染，再使用下一次配置；排队取消不启动新渲染', async () => {
    let finish!: (value: { svg: string }) => void
    let started!: () => void
    const ready = new Promise<void>(resolve => { started = resolve })
    engine.render.mockImplementationOnce(() => {
      started()
      return new Promise<{ svg: string }>(resolve => { finish = resolve })
    })
    const controller = new AbortController()
    const first = renderDiagram('flowchart LR\nA-->B', new AbortController().signal)
    await ready
    const second = renderDiagram('flowchart LR\nC-->D', controller.signal)
    const rejected = expect(second).rejects.toThrow()
    controller.abort()
    finish({ svg })
    await first
    await rejected
    expect(engine.render).toHaveBeenCalledTimes(1)
    expect(document.querySelector('.mermaid-render-host')).toBeNull()
  })

  it('语法失败清理临时节点，后续图表仍能正常显示', async () => {
    engine.parse.mockRejectedValueOnce(new Error('parse'))
    await expect(renderDiagram('flowchart LR\n[', new AbortController().signal)).rejects.toThrow('syntax')
    await expect(renderDiagram('flowchart LR\nA-->B', new AbortController().signal)).resolves.toMatchObject({ width: 300 })
    expect(document.querySelector('.mermaid-render-host')).toBeNull()
  })

  it('不支持的图表不进入渲染器', async () => {
    engine.detectType.mockReturnValue('architecture')
    await expect(renderDiagram('architecture-beta', new AbortController().signal)).rejects.toThrow('unsupported')
    expect(engine.render).not.toHaveBeenCalled()
  })

  it('图表模块加载失败保留重试，异常 SVG 不归为源码语法错误', async () => {
    engine.parse.mockRejectedValueOnce(new TypeError('Failed to fetch dynamically imported module'))
    await expect(renderDiagram('flowchart LR\nA-->B', new AbortController().signal)).rejects.toThrow('load')
    engine.render.mockResolvedValueOnce({ svg: '<svg viewBox="0 0 0 0"/>' })
    await expect(renderDiagram('flowchart LR\nA-->B', new AbortController().signal)).rejects.toThrow('render')
  })
})
