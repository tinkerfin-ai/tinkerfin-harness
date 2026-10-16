import DOMPurify from 'dompurify'
import type { Mermaid } from 'mermaid'

export type DiagramErrorKind = 'unsupported' | 'unsafe' | 'too-large' | 'syntax' | 'load' | 'render'
export class DiagramError extends Error {
  constructor(readonly kind: DiagramErrorKind, cause?: unknown) { super(kind, { cause }) }
}
export interface RenderedDiagram {
  url: string
  width: number
  height: number
  description: string
}

let engine: Promise<Mermaid> | undefined
let queue: Promise<unknown> = Promise.resolve()
let nextId = 0
const supported = new Set(['flowchart', 'flowchart-v2', 'sequence', 'class', 'classDiagram', 'state', 'stateDiagram', 'er', 'gantt', 'pie', 'mindmap'])

export function validateDiagramSource(source: string) {
  if (source.length > 50_000) throw new DiagramError('too-large')
  // 用户图表只描述内容，不修改宿主配置，也不加载图片、字体或其他远端资源
  if (/%%\s*\{|^\s*---|\b(?:img|image)\s*:|url\s*\(|@import|^\s*(?:classDef|linkStyle|style)\b[^\n]*\\/im.test(source)) {
    throw new DiagramError('unsafe')
  }
}

function loadEngine() {
  engine ??= import('mermaid').then(module => module.default).catch(() => {
    engine = undefined
    throw new DiagramError('load')
  })
  return engine
}

/** 所有图表共享引擎；配置与渲染串行执行，取消的调用不提交结果 */
export function renderDiagram(source: string, signal: AbortSignal): Promise<RenderedDiagram> {
  const render = async () => {
    signal.throwIfAborted()
    validateDiagramSource(source)
    const mermaid = await loadEngine()
    signal.throwIfAborted()
    const styles = getComputedStyle(document.documentElement)
    const token = (name: string) => styles.getPropertyValue(name).trim()
    mermaid.initialize({
      startOnLoad: false, securityLevel: 'strict', htmlLabels: false,
      suppressErrorRendering: true, maxTextSize: 50_000, maxEdges: 500,
      theme: 'base', fontFamily: token('--font-ui'),
      secure: ['securityLevel', 'startOnLoad', 'htmlLabels', 'theme', 'themeCSS', 'themeVariables', 'fontFamily', 'maxTextSize', 'maxEdges'],
      themeVariables: {
        darkMode: document.documentElement.dataset.theme === 'dark',
        fontFamily: token('--font-ui'), fontSize: token('--type-ui-size'),
        primaryColor: token('--color-brand-soft'), primaryTextColor: token('--color-text-primary'),
        primaryBorderColor: token('--color-text-tertiary'),
        secondaryColor: token('--color-layer-2'), tertiaryColor: token('--color-subtle'),
        lineColor: token('--color-text-secondary'), textColor: token('--color-text-primary'),
        mainBkg: token('--color-layer-2'), nodeBorder: token('--color-text-tertiary'),
        clusterBkg: token('--color-layer-2'), clusterBorder: token('--color-text-tertiary'),
        edgeLabelBackground: token('--color-canvas'), background: token('--color-canvas'),
      },
    })
    let type: string
    try { type = mermaid.detectType(source) } catch { throw new DiagramError('unsupported') }
    if (!supported.has(type)) throw new DiagramError('unsupported')
    try { await mermaid.parse(source) } catch (error) {
      // 解析会按需加载图表模块；浏览器加载失败不能被当作用户源码错误
      throw new DiagramError(error instanceof TypeError ? 'load' : 'syntax', error)
    }
    signal.throwIfAborted()
    const host = document.createElement('div')
    host.className = 'mermaid-render-host'
    host.setAttribute('aria-hidden', 'true')
    host.inert = true
    document.body.append(host)
    try {
      const { svg } = await mermaid.render(`studio-diagram-${++nextId}`, source, host)
      signal.throwIfAborted()
      return diagramImage(svg)
    } catch (error) {
      if (signal.aborted || error instanceof DiagramError) throw error
      throw new DiagramError('render', error)
    } finally {
      host.remove()
    }
  }
  const result = queue.then(render, render)
  queue = result.catch(() => undefined)
  return result
}

/** SVG 以图片方式展示，源码不获得页面事件、链接导航或样式作用域 */
export function diagramImage(svg: string): RenderedDiagram {
  const clean = DOMPurify.sanitize(svg, {
    USE_PROFILES: { svg: true, svgFilters: true },
    FORBID_TAGS: ['foreignObject', 'image', 'script', 'a', 'animate', 'set'],
  })
  const document = new DOMParser().parseFromString(clean, 'image/svg+xml')
  const element = document.documentElement
  if (element.tagName.toLowerCase() !== 'svg') throw new DiagramError('render')
  const viewBox = element.getAttribute('viewBox')?.split(/[\s,]+/).map(Number)
  const width = viewBox?.[2] ?? Number.parseFloat(element.getAttribute('width') ?? '')
  const height = viewBox?.[3] ?? Number.parseFloat(element.getAttribute('height') ?? '')
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) throw new DiagramError('render')
  element.setAttribute('xmlns', 'http://www.w3.org/2000/svg')
  element.setAttribute('width', String(width))
  element.setAttribute('height', String(height))
  const description = [element.querySelector('title')?.textContent, element.querySelector('desc')?.textContent].filter(Boolean).join(' · ')
  return { url: `data:image/svg+xml;charset=utf-8,${encodeURIComponent(new XMLSerializer().serializeToString(element))}`, width, height, description }
}
