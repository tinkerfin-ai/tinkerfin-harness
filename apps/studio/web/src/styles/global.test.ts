import { describe, expect, it } from 'vitest'

import indexHtml from '../../index.html?raw'
import mainEntry from '../main.tsx?raw'
import tokensStyles from './tokens.css?raw'
import fontsStyles from './fonts.css?raw'

const cssFiles = import.meta.glob('../**/*.css', {
  eager: true,
  import: 'default',
  query: '?raw',
}) as Record<string, string>

const componentStyles = Object.entries(cssFiles)
  .filter(([path]) => !path.endsWith('/tokens.css') && !path.endsWith('/fonts.css'))
  .map(([, source]) => source)
  .join('\n')

const declarations = (block: string) => new Map(
  [...block.matchAll(/(--[\w-]+):\s*([^;]+);/g)].map((match) => [match[1], match[2].trim()]),
)

const themeBlocks = () => {
  const light = tokensStyles.match(/:root\s*{([^}]*)}/s)?.[1] ?? ''
  const dark = tokensStyles.match(/:root\[data-theme='dark'\]\s*{([^}]*)}/s)?.[1] ?? ''
  const lightTokens = declarations(light)
  const darkTokens = new Map([...lightTokens, ...declarations(dark)])
  return [lightTokens, darkTokens]
}

const resolveToken = (tokens: Map<string, string>, name: string, seen = new Set<string>()): string => {
  if (seen.has(name)) throw new Error(`令牌循环引用：${name}`)
  const value = tokens.get(name)
  expect(value, `缺少令牌 ${name}`).toBeDefined()
  const reference = value?.match(/^var\((--[\w-]+)\)$/)?.[1]
  if (!reference) return value ?? ''
  seen.add(name)
  return resolveToken(tokens, reference, seen)
}

const relativeLuminance = (hexColor: string) => {
  const channels = (hexColor.slice(1).match(/.{2}/g) ?? [])
    .map((channel) => Number.parseInt(channel, 16) / 255)
    .map((channel) => channel <= 0.04045
      ? channel / 12.92
      : ((channel + 0.055) / 1.055) ** 2.4)
  return (0.2126 * channels[0]) + (0.7152 * channels[1]) + (0.0722 * channels[2])
}

const contrastRatio = (firstColor: string, secondColor: string) => {
  const first = relativeLuminance(firstColor)
  const second = relativeLuminance(secondColor)
  return (Math.max(first, second) + 0.05) / (Math.min(first, second) + 0.05)
}

const blendColor = (foreground: string, background: string, alpha: number) => (
  '#' + [1, 3, 5].map((offset) => Math.round(
    Number.parseInt(foreground.slice(offset, offset + 2), 16) * alpha
    + Number.parseInt(background.slice(offset, offset + 2), 16) * (1 - alpha),
  ).toString(16).padStart(2, '0')).join('')
)

describe('前端视觉契约', () => {
  it('每个 CSS owner 恰好归属一个预声明 layer', () => {
    const layerOrder = tokensStyles.match(/@layer\s+([^;]+);/)?.[1]
      .split(',')
      .map((layer) => layer.trim()) ?? []
    expect(layerOrder).toEqual([
      'tokens',
      'base',
      'ui',
      'auth',
      'conversation',
      'workspace',
      'settings',
      'utilities',
    ])

    for (const [path, source] of Object.entries(cssFiles)) {
      const owners = [...source.matchAll(/@layer\s+([\w-]+)\s*\{/g)]
        .map((match) => match[1])
      expect([...new Set(owners)], `${path} 必须只有一个 owner layer`).toHaveLength(1)
      expect(layerOrder, `${path} 使用了未预声明 layer`).toContain(owners[0])
    }
  })

  it('只为西文字体声明实际使用的 Latin 变量文件', () => {
    expect(fontsStyles.match(/@font-face/g)).toHaveLength(3)
    expect(fontsStyles).toContain('inter-latin-wght-normal.woff2')
    expect(fontsStyles).toContain('inter-latin-wght-italic.woff2')
    expect(fontsStyles).toContain('jetbrains-mono-latin-wght-normal.woff2')
    expect(fontsStyles).not.toMatch(/latin-ext|cyrillic|greek|vietnamese/)
    expect(mainEntry).toContain("import '@fontsource-variable/noto-sans-sc'")
  })

  it('浅色和深色普通文本、辅助文本及状态文本均达到 4.5:1', () => {
    for (const tokens of themeBlocks()) {
      for (const [foreground, background] of [
        ['--color-text-primary', '--color-canvas'],
        ['--color-text-secondary', '--color-canvas'],
        ['--color-text-tertiary', '--color-canvas'],
        ['--color-text-caption', '--color-canvas'],
        ['--color-placeholder', '--color-layer-2'],
        ['--color-brand-text', '--color-brand-soft'],
        ['--color-on-selection', '--color-selection'],
        ['--color-danger-text', '--color-danger-soft'],
        ['--color-success-text', '--color-success-soft'],
        ['--color-warning-text', '--color-warning-soft'],
        ['--color-text-caption-on-layer', '--color-layer-1'],
        ['--color-text-caption-on-layer', '--color-layer-2'],
        ['--color-text-caption-on-layer', '--color-code-surface'],
      ]) {
        const foregroundColor = resolveToken(tokens, foreground)
        const backgroundColor = resolveToken(tokens, background)
        expect(foregroundColor).toMatch(/^#[0-9a-f]{6}$/i)
        expect(backgroundColor).toMatch(/^#[0-9a-f]{6}$/i)
        expect(contrastRatio(foregroundColor, backgroundColor)).toBeGreaterThanOrEqual(4.5)
      }
    }
  })

  it('链路六类节点在浅深主题中使用唯一且可读的语义色', () => {
    const traceColors = [
      '--color-trace-user',
      '--color-trace-context',
      '--color-trace-model',
      '--color-trace-tool',
      '--color-trace-subagent',
      '--color-trace-assistant',
    ]
    for (const tokens of themeBlocks()) {
      const canvas = resolveToken(tokens, '--color-canvas')
      const colors = traceColors.map((name) => resolveToken(tokens, name))
      expect(new Set(colors).size).toBe(traceColors.length)
      for (const color of colors) {
        expect(color).toMatch(/^#[0-9a-f]{6}$/i)
        expect(contrastRatio(color, canvas)).toBeGreaterThanOrEqual(4.5)
      }
      for (const [index, color] of colors.entries()) {
        const alpha = [0.1, 0.12, 0.1, 0.12, 0.1, 0.1][index]
        for (const background of [canvas, resolveToken(tokens, '--color-brand-soft')]) {
          expect(contrastRatio(color, blendColor(color, background, alpha)), traceColors[index])
            .toBeGreaterThanOrEqual(4.5)
        }
      }
    }
  })

  it('所有功能 CSS 入口都受扫描且不直接声明十六进制色或原始色令牌', () => {
    expect(Object.keys(cssFiles)).toEqual(expect.arrayContaining([
      '../components/ui/ui.css',
      '../features/auth/auth.css',
      '../features/conversation/conversation.css',
      '../features/settings/settings.css',
      '../features/workspace/workspace.css',
      './global.css',
      './tokens.css',
      './typography.css',
    ]))
    expect(componentStyles).not.toMatch(/#[0-9a-f]{3,8}\b/i)
    expect(componentStyles).not.toMatch(/var\(--primitive-/)

    const unregisteredRadii = [...componentStyles.matchAll(/border-radius:\s*([^;]+);/g)]
      .map((match) => match[1].trim())
      .filter((value) => !value.includes('var(--radius-') && !['0', 'inherit'].includes(value))
    const unregisteredShadows = [...componentStyles.matchAll(/box-shadow:\s*([^;]+);/g)]
      .map((match) => match[1].trim())
      .filter((value) => value !== 'none' && !value.includes('var(--shadow-'))
    const unregisteredLayers = [...componentStyles.matchAll(/z-index:\s*([^;]+);/g)]
      .map((match) => match[1].trim())
      .filter((value) => !value.includes('var(--layer-'))
    const breakpointValues = [...new Set(
      [...componentStyles.matchAll(/@media \((?:min|max)-width:\s*(\d+)px\)/g)]
        .map((match) => match[1]),
    )].sort((left, right) => Number(left) - Number(right))
    const ownerLocalMotionValues = [...new Set(
      [...componentStyles.matchAll(/(?<![-\w.])(?:\d*\.)?\d+(?:ms|s)\b/g)]
        .map((match) => match[0]),
    )].sort()

    expect(unregisteredRadii).toEqual([])
    expect(unregisteredShadows).toEqual([])
    expect(unregisteredLayers).toEqual([])
    expect(breakpointValues).toEqual(['440', '767', '920', '1023', '1281'])
    expect(ownerLocalMotionValues).toEqual([])
  })

  it('字号和字重通过排版角色消费，不在功能样式中形成第二套尺度', () => {
    const explicitFontSizes = [...componentStyles.matchAll(/font-size:\s*([^;]+);/g)]
      .map((match) => match[1].trim())
      .filter((value) => !['0', 'inherit'].includes(value) && !value.startsWith('var(--type-'))
    const explicitFontWeights = [...componentStyles.matchAll(/font-weight:\s*([^;]+);/g)]
      .map((match) => match[1].trim())
      .filter((value) => !value.startsWith('var(--weight-'))
    const interactiveCaptionSelectors = [...componentStyles.matchAll(
      /([^{}]+)\{[^{}]*font-size:\s*var\(--type-caption-size\);/g,
    )]
      .map((match) => match[1].trim())
      .filter((selector) => /button|\.ui-button|input|textarea|select|\[role=/.test(selector))

    expect(explicitFontSizes).toEqual([])
    expect(explicitFontWeights).toEqual([])
    expect(interactiveCaptionSelectors).toEqual([])
    expect(cssFiles['./global.css']).toMatch(/button,[\s\S]*select\s*{\s*font-size:\s*inherit;/)
  })

  it('共享控件覆盖焦点、禁用、触控、forced-colors 与 reduced-motion', () => {
    const uiStyles = cssFiles['../components/ui/ui.css']
    expect(tokensStyles).toContain('--motion-tooltip-hide-delay: 0ms;')
    expect(uiStyles).toMatch(/\.ui-tooltip\s*\{[^}]*opacity:\s*0;[^}]*opacity var\(--motion-instant\) linear var\(--motion-tooltip-hide-delay\)/s)
    expect(uiStyles).toMatch(/\.ui-button:focus-visible[\s\S]*outline:/)
    expect(uiStyles).toMatch(/\.ui-button:disabled[\s\S]*opacity:/)
    expect(uiStyles).toMatch(/@media \(any-hover: none\), \(any-pointer: coarse\)[\s\S]*--control-lg/)
    expect(componentStyles).toContain('@media (forced-colors: active)')
    expect(componentStyles).toContain('@media (prefers-reduced-motion: reduce)')
  })

  it('保留页面缩放并在应用执行前解析主题', () => {
    const bootstrapPosition = indexHtml.indexOf('tinkerfin:theme')
    const applicationPosition = indexHtml.indexOf('/src/main.tsx')
    expect(indexHtml).toContain('width=device-width, initial-scale=1.0')
    expect(indexHtml).not.toContain('user-scalable=no')
    expect(bootstrapPosition).toBeGreaterThan(-1)
    expect(bootstrapPosition).toBeLessThan(applicationPosition)
    expect(indexHtml).toContain("'#151517'")
  })
})
