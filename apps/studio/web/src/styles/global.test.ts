import { describe, expect, it } from 'vitest'

import indexHtml from '../../index.html?raw'
import mainEntry from '../main.tsx?raw'
import workspaceLayoutAnimation from '../features/workspace/useWorkspaceLayoutAnimation.ts?raw'
import tokensStyles from './tokens.css?raw'
import typographyStyles from './typography.css?raw'
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

  it('按既定顺序加载自托管字体、令牌、排版和基础样式', () => {
    const imports = [
      './styles/fonts.css',
      '@fontsource-variable/noto-sans-sc',
      './styles/tokens.css',
      './styles/typography.css',
      './styles/global.css',
      './components/ui/ui.css',
    ].map((path) => mainEntry.indexOf(`import '${path}'`))

    expect(imports.every((index) => index >= 0)).toBe(true)
    expect(imports).toEqual([...imports].sort((left, right) => left - right))
    expect(mainEntry).not.toContain('@fontsource-variable/geist')
  })

  it('定义 Inter、Noto Sans SC、JetBrains Mono 和四级字重', () => {
    expect(typographyStyles).toContain(
      "--font-ui: 'Inter Variable', 'Noto Sans SC Variable', 'PingFang SC', 'Microsoft YaHei UI', system-ui, sans-serif;",
    )
    expect(typographyStyles).toContain(
      "--font-code: 'JetBrains Mono Variable', 'SFMono-Regular', Consolas, 'Liberation Mono', monospace;",
    )
    for (const weight of [400, 500, 600, 700]) {
      expect(typographyStyles).toContain(`: ${weight};`)
    }
    expect(typographyStyles).toMatch(/--type-body-size:\s*16px;[\s\S]*--type-body-line:\s*28px;/)
    expect(typographyStyles).toContain('--type-composer-line: 24px;')
    expect(typographyStyles).toMatch(/--type-h1-size:\s*24px;[\s\S]*--type-h1-line:\s*34px;/)
    expect(typographyStyles).toMatch(/--type-brand-size:\s*26px;[\s\S]*--type-brand-line:\s*32px;/)
  })

  it('只为西文字体声明实际使用的 Latin 变量文件', () => {
    expect(fontsStyles.match(/@font-face/g)).toHaveLength(3)
    expect(fontsStyles).toContain('inter-latin-wght-normal.woff2')
    expect(fontsStyles).toContain('inter-latin-wght-italic.woff2')
    expect(fontsStyles).toContain('jetbrains-mono-latin-wght-normal.woff2')
    expect(fontsStyles).not.toMatch(/latin-ext|cyrillic|greek|vietnamese/)
    expect(mainEntry).toContain("import '@fontsource-variable/noto-sans-sc'")
  })

  it('锁定布局、控件、圆角、层级与 100/200/300ms 动效尺度', () => {
    for (const declaration of [
      '--layout-sidebar-expanded: 261px;',
      '--layout-sidebar-rail: 56px;',
      '--layout-content-wide: 840px;',
      '--layout-drawer-width: 400px;',
      '--layout-header-height: var(--space-16);',
      '--layout-drawer-header-height: calc(var(--layout-header-height) + var(--space-3));',
      '--layout-settings-dialog: 760px;',
      '--layout-settings-nav: 180px;',
      '--layout-settings-height: 540px;',
      '--layout-composer-surface-height: 118px;',
      '--control-lg: 44px;',
      '--control-plan-chip: 24px;',
      '--control-composer: 34px;',
      '--radius-3xl: 22px;',
      '--layer-local: 1;',
      '--layer-local-raised: 2;',
      '--motion-fast: 100ms;',
      '--motion-normal: 200ms;',
      '--motion-slow: 300ms;',
      '--motion-loading-cycle: 1000ms;',
      '--motion-tool-result-cycle: 1450ms;',
      '--motion-tool-sweep-cycle: 2600ms;',
      '--motion-attention-cycle: 1800ms;',
      '--layout-sidebar-expanded: 261px;',
    ]) expect(tokensStyles).toContain(declaration)
  })

  it('操作型弹窗使用紧凑按钮并让关闭图标对齐标题行', () => {
    const uiStyles = cssFiles['../components/ui/ui.css']

    expect(uiStyles).toMatch(/\.modal-dialog--action\s*\{[^}]*width:\s*min\(100%, 400px\);/s)
    expect(uiStyles).toMatch(/\.modal-dialog--action \.modal-dialog-head\s*\{[^}]*align-items:\s*flex-start;[^}]*padding:\s*var\(--space-5\) var\(--space-5\) 0;/s)
    expect(uiStyles).toMatch(/\.modal-dialog--action \.modal-dialog-head > \.ui-icon-button-wrap\s*\{[^}]*margin-block:\s*calc\(0px - var\(--space-1\)\);[^}]*margin-inline-end:\s*calc\(0px - var\(--space-2\)\);/s)
    expect(uiStyles).toMatch(/\.modal-dialog--action form\s*\{[^}]*padding:\s*var\(--space-6\) var\(--space-5\) var\(--space-5\);/s)
    expect(uiStyles).toMatch(/\.modal-dialog--action \.modal-dialog-actions\s*\{[^}]*gap:\s*var\(--space-2\);[^}]*margin-top:\s*0;/s)
    expect(uiStyles).toMatch(/\.modal-dialog--action \.modal-dialog-error \+ \.modal-dialog-actions\s*\{[^}]*margin-top:\s*var\(--space-3\);/s)
    expect(uiStyles).toMatch(/\.modal-dialog--action\.has-input \.modal-dialog-actions\s*\{[^}]*margin-top:\s*var\(--space-6\);/s)
    expect(uiStyles).not.toMatch(/\.modal-dialog--action \.modal-dialog-actions \.ui-button\s*\{/s)
    expect(uiStyles).toMatch(/@media \(any-hover: none\), \(any-pointer: coarse\)[\s\S]*\.ui-button\s*\{[^}]*min-width:\s*var\(--control-lg\);[^}]*min-height:\s*var\(--control-lg\);/s)
  })

  it('工作区状态和全局提示使用统一反馈卡片宽度', () => {
    const uiStyles = cssFiles['../components/ui/ui.css']

    expect(tokensStyles).toContain('--layout-feedback-card: 288px;')
    expect(uiStyles).toMatch(/\.ui-feedback-state\s*\{[^}]*width:\s*min\(calc\(100vw - var\(--space-6\)\), var\(--layout-feedback-card\)\);/s)
    expect(uiStyles).toMatch(/\.toast-viewport\s*\{[^}]*width:\s*min\(calc\(100vw - var\(--space-6\)\), var\(--layout-feedback-card\)\);/s)
    expect(uiStyles).toMatch(/\.toast-card\s*\{[^}]*width:\s*100%;[^}]*max-width:\s*100%;/s)
    expect(uiStyles).toMatch(/\.ui-feedback-state\s*\{[^}]*grid-template-columns:\s*var\(--control-lg\) minmax\(0, 1fr\) var\(--control-lg\);/s)
    expect(uiStyles).toMatch(/\.toast-card\s*\{[^}]*grid-template-columns:\s*var\(--control-lg\) minmax\(0, 1fr\) var\(--control-lg\);/s)
    expect(uiStyles).toMatch(/\.ui-feedback-state__title\s*\{[^}]*text-align:\s*center;/s)
    expect(uiStyles).toMatch(/\.toast-card p\s*\{[^}]*text-align:\s*center;/s)
    expect(uiStyles).toMatch(/\.ui-feedback-state > \.ui-icon-button-wrap\s*\{[^}]*translate:\s*calc\(var\(--space-2\) - 1px\) 0;/s)
    expect(uiStyles).toMatch(/\.toast-card > button\s*\{[^}]*translate:\s*var\(--space-2\) 0;/s)
    expect(uiStyles).toMatch(/\.ui-feedback-state__retry,[\s\S]*?border:\s*0;[^}]*background:\s*transparent;[^}]*box-shadow:\s*none;/s)
  })

  it('所有实际抽屉使用同一宽度、头部高度、标题层级和关闭按钮布局', () => {
    const uiStyles = cssFiles['../components/ui/ui.css']
    const todoTraceStyles = cssFiles['../features/conversation/todoTrace/todoTrace.css']
    const chainTraceStyles = cssFiles['../features/conversation/chainTrace/chainTrace.css']

    expect(uiStyles).toMatch(/\.ui-drawer-header\s*\{[^}]*height:\s*var\(--layout-drawer-header-height\);[^}]*padding:\s*calc\(var\(--space-2\) \+ var\(--space-4\)\) 0 var\(--space-2\);/s)
    expect(uiStyles).toMatch(/\.ui-drawer-header > \.ui-icon-button-wrap\s*\{[^}]*grid-row:\s*1;/s)
    expect(tokensStyles).toContain('--layout-drawer-width: 400px;')
    expect(todoTraceStyles).toMatch(/\.todo-trace-drawer\s*\{[^}]*grid-template-rows:\s*var\(--layout-drawer-header-height\) minmax\(0, 1fr\);/s)
    expect(todoTraceStyles).toMatch(/\.todo-trace-drawer\s*\{[^}]*width:\s*min\(var\(--layout-drawer-width\), 100vw\);/s)
    expect(todoTraceStyles).toMatch(/\.todo-trace-scroll\s*\{[^}]*overflow-y:\s*auto;[^}]*overscroll-behavior-y:\s*none;[^}]*overscroll-behavior-x:\s*auto;/s)
    expect(chainTraceStyles).toMatch(/\.chain-trace-ledger\s*\{[^}]*padding-bottom:\s*var\(--chain-trace-turn-height\);/s)
    expect(chainTraceStyles).toMatch(/\.chain-trace-details\s*\{[^}]*position:\s*relative;[^}]*grid-template-rows:\s*var\(--space-16\) var\(--chain-trace-detail-tabs-height\) minmax\(0, 1fr\);/s)
    expect(chainTraceStyles).toMatch(/\.chain-trace-details\s*\{[^}]*width:\s*var\(--layout-drawer-width\);/s)
    expect(chainTraceStyles).toMatch(/\.chain-trace-ledger\s*\{[^}]*overflow:\s*auto;[^}]*overscroll-behavior-y:\s*none;[^}]*overscroll-behavior-x:\s*auto;/s)
    expect(chainTraceStyles).toMatch(/\.chain-trace-detail-body\s*\{[^}]*overflow:\s*auto;[^}]*overscroll-behavior-y:\s*none;[^}]*overscroll-behavior-x:\s*auto;/s)
    expect(uiStyles).toMatch(/\.modal-backdrop\s*\{[^}]*position:\s*fixed;[^}]*z-index:\s*var\(--layer-modal\);[^}]*inset:\s*0;/s)
    expect(chainTraceStyles).toMatch(/\.chain-trace-details-backdrop\s*\{[^}]*place-items:\s*stretch;[^}]*justify-items:\s*end;[^}]*padding:\s*0;/s)
    expect(chainTraceStyles).toMatch(/\.chain-trace-details-backdrop \.chain-trace-details\s*\{[^}]*width:\s*min\(var\(--layout-drawer-width\), calc\(100vw - var\(--space-8\)\)\);[^}]*height:\s*100dvh;/s)
  })

  it('共享 Button 与 TextField 使用统一桌面尺度和单一中性焦点边', () => {
    const uiStyles = cssFiles['../components/ui/ui.css']
    const conversationStyles = cssFiles['../features/conversation/conversation.css']

    expect(uiStyles).toMatch(/\.ui-button--xs\s*\{[^}]*--button-control-size:\s*var\(--control-xs\);[^}]*min-height:\s*var\(--button-control-size\);/s)
    expect(uiStyles).toMatch(/\.ui-button--circle\s*\{[^}]*width:\s*var\(--button-control-size\);[^}]*min-width:\s*var\(--button-control-size\);/s)
    expect(uiStyles).toMatch(/\.ui-text-field--md \.ui-text-field__control\s*\{[^}]*min-height:\s*var\(--control-md\);/s)
    expect(uiStyles).toMatch(/\.ui-text-field--lg \.ui-text-field__control\s*\{[^}]*min-height:\s*var\(--control-lg\);/s)
    expect(uiStyles).toMatch(/\.ui-text-field__control:focus-within,\s*\.ui-temporal-picker__trigger:focus-visible\s*\{[^}]*border-color:\s*var\(--color-border-strong\);[^}]*box-shadow:\s*none;/s)
    expect(tokensStyles).not.toContain('--shadow-focus')
    expect(conversationStyles).toMatch(/\.approval-rejection-form textarea:focus,\s*\.plan-review-rejection-form textarea:focus\s*\{[^}]*border-color:\s*var\(--color-border-strong\);[^}]*outline:\s*0;[^}]*box-shadow:\s*none;/s)
    expect(conversationStyles).not.toMatch(/\.approval-rejection-form textarea:focus-visible[^{]*\{[^}]*var\(--color-focus\)/s)
    expect(conversationStyles).toMatch(/@media \(forced-colors:\s*active\)[\s\S]*\.approval-rejection-form textarea:focus-visible,[\s\S]*\.plan-review-rejection-form textarea:focus-visible,[\s\S]*\.composer-attachment-remove:focus-visible\s*\{[^}]*outline:\s*2px solid Highlight;/s)
  })

  it('带边框控件使用单一焦点边且鼠标焦点不触发主题选项轮廓', () => {
    const uiStyles = cssFiles['../components/ui/ui.css']
    const settingsStyles = cssFiles['../features/settings/settings.css']

    expect(uiStyles).toMatch(/\.ui-button:focus-visible\s*\{[^}]*outline:\s*2px solid var\(--color-focus\);[^}]*outline-offset:\s*0;/s)
    expect(uiStyles).toMatch(/\.theme-switcher-input:focus-visible \+ \.theme-switcher-visual\s*\{[^}]*outline-offset:\s*0;[^}]*box-shadow:\s*none;/s)
    expect(settingsStyles).not.toContain('.settings-theme-option:focus-within')
    expect(settingsStyles).toMatch(/\.settings-theme-option:has\(input:focus-visible\)\s*\{[^}]*outline:\s*2px solid var\(--color-focus\);[^}]*outline-offset:\s*0;/s)
    expect(settingsStyles).toMatch(/\.settings-choice-trigger:focus-visible\s*\{[^}]*outline:\s*2px solid var\(--color-focus\);[^}]*outline-offset:\s*0;/s)
  })

  it('会话卡片键盘焦点不使用品牌蓝色边框', () => {
    const conversationStyles = cssFiles['../features/conversation/conversation.css']
    const uiStyles = cssFiles['../components/ui/ui.css']
    expect(conversationStyles).toMatch(/\.subagent-card-head:focus-visible\s*\{[^}]*outline:\s*2px solid var\(--color-border-strong\);/s)
    expect(conversationStyles).toMatch(/\.tool-row > summary:focus-visible\s*\{[^}]*outline:\s*2px solid var\(--color-border-strong\);/s)
    expect(conversationStyles).toMatch(/\.plan-interaction-toggle-surface:focus-visible\s*\{[^}]*outline:\s*2px solid var\(--color-border-strong\);/s)
    expect(conversationStyles).not.toContain('.approval-toggle-surface')
    expect(conversationStyles).toMatch(/\.plan-question-progress-step:focus-visible\s*\{[^}]*outline:\s*2px solid var\(--color-border-strong\);/s)
    expect(uiStyles).toMatch(/\.ui-text-field__control:focus-within,\s*\.ui-temporal-picker__trigger:focus-visible\s*\{[^}]*border-color:\s*var\(--color-border-strong\);[^}]*outline:\s*0;/s)
    expect(conversationStyles).toMatch(/:is\(\.approval-composer, \.plan-question-composer, \.plan-review-composer\)[\s\S]*\.ui-button:not\(\.ui-icon-button\):focus-visible\s*\{[^}]*outline:\s*2px solid var\(--color-border-strong\);/s)
  })

  it('会话正文与输入卡片使用独立的 DSH 对齐宽度', () => {
    const workspaceStyles = cssFiles['../features/workspace/workspace.css']
    const conversationStyles = cssFiles['../features/conversation/conversation.css']

    expect(tokensStyles).toContain('--layout-conversation-width: 748px;')
    expect(tokensStyles).toContain('--layout-composer-width: 780px;')
    expect(tokensStyles).toContain('--layout-composer-surface-height: 118px;')
    expect(tokensStyles).toContain('--layout-interaction-card-min-height: clamp(260px, 32dvh, 320px);')
    expect(tokensStyles).toContain('--layout-interaction-card-context-reserve: 120px;')
    expect(tokensStyles).toContain('--layout-interaction-card-max-cap: 680px;')
    expect(tokensStyles).toContain('--layout-interaction-card-max-height: clamp(var(--layout-interaction-card-min-height), calc(100dvh - var(--layout-header-height) - var(--layout-interaction-card-context-reserve) - var(--space-10)), var(--layout-interaction-card-max-cap));')
    expect(tokensStyles).toContain('--layout-interaction-card-gap: var(--space-6);')
    expect(tokensStyles).not.toContain('--layout-interaction-card-height:')
    expect(workspaceStyles).toMatch(/\.conversation-pane\s*\{[^}]*padding-inline:\s*var\(--space-8\);/s)
    expect(workspaceStyles).toMatch(/\.message-list\s*\{[^}]*width:\s*min\(100%, var\(--layout-conversation-width\)\)/s)
    expect(workspaceStyles).toMatch(/\.message-list\s*\{[^}]*padding:\s*var\(--space-8\) 0 var\(--composer-height\);/s)
    expect(workspaceStyles).toMatch(/\.workspace-main:has\(\.composer-dock\.is-taken-over\)\s*\{[^}]*grid-template-rows:\s*var\(--layout-header-height\) minmax\(0, 1fr\) auto;/s)
    expect(workspaceStyles).toMatch(/\.workspace-main:has\(\.composer-dock\.is-taken-over\) \.message-list\s*\{[^}]*padding-bottom:\s*var\(--space-4\);/s)
    expect(workspaceStyles).not.toMatch(/\.message-list\s*\{[^}]*padding-(?:right|left):/s)
    expect(workspaceStyles).toMatch(/@media \(max-width:\s*767px\)[\s\S]*\.conversation-pane\s*\{[^}]*padding-inline:\s*var\(--space-4\);[^}]*\}[\s\S]*\.message-list\s*\{[^}]*padding-top:\s*var\(--space-6\);/s)
    expect(workspaceStyles).toMatch(/@media \(max-width:\s*440px\)[\s\S]*\.conversation-pane\s*\{[^}]*padding-inline:\s*var\(--space-3\);/s)
    expect(conversationStyles).toMatch(/\.composer\s*\{[^}]*width:\s*min\(100%, var\(--layout-composer-width\)\);[^}]*min-height:\s*var\(--layout-composer-surface-height\);/s)
    expect(conversationStyles).toMatch(/\.approval-composer,[\s\S]*\.plan-review-composer\s*\{[^}]*width:\s*min\(100%, var\(--layout-composer-width\)\);/s)
    expect(conversationStyles).toMatch(/\.approval-composer,[\s\S]*\.plan-review-composer\s*\{[^}]*height:\s*var\(--interaction-card-height, var\(--layout-interaction-card-min-height\)\);[^}]*min-height:\s*var\(--layout-interaction-card-min-height\);[^}]*max-height:\s*var\(--layout-interaction-card-max-height\);/s)
    expect(conversationStyles).toMatch(/\.composer-dock\.is-taken-over\s*\{[^}]*--composer-top-inset:\s*var\(--layout-interaction-card-gap\);[^}]*grid-row:\s*3;/s)
    expect(conversationStyles).toMatch(/\.composer-dock\.is-taken-over::before\s*\{[^}]*display:\s*none;/s)
    expect(conversationStyles).toMatch(/\.plan-question-composer\.is-minimized\s*\{[^}]*height:\s*auto;[^}]*min-height:\s*var\(--layout-composer-surface-height\);[^}]*max-height:\s*none;/s)
    const minimizedSurfaceRule = conversationStyles.match(
      /\.plan-question-composer\.is-minimized\s*\{[^}]*\}/s,
    )?.[0] ?? ''
    expect(minimizedSurfaceRule).toMatch(/(?:^|\n)\s*height:\s*auto;/)
    expect(conversationStyles).not.toContain('.approval-composer.is-minimized')
    expect(conversationStyles).not.toContain('.plan-review-composer.is-minimized')
  })

  it('交互卡片无外边框且上边框悬浮只改变拖拽光标', () => {
    const conversationStyles = cssFiles['../features/conversation/conversation.css']

    expect(conversationStyles).toMatch(/\.approval-composer,\s*\.plan-question-composer,\s*\.plan-review-composer\s*\{[^}]*border:\s*0;/s)
    expect(tokensStyles).not.toContain('--color-plan-panel-border')
    expect(tokensStyles).not.toContain('--color-warning-panel-border')
    expect(conversationStyles).toMatch(/\.interaction-card-resize-handle\s*\{[^}]*display:\s*none;[^}]*top:\s*-1px;[^}]*height:\s*var\(--space-3\);[^}]*cursor:\s*ns-resize;[^}]*touch-action:\s*none;/s)
    expect(conversationStyles).toMatch(/@media \(hover:\s*hover\) and \(pointer:\s*fine\)[\s\S]*\.interaction-card-resize-handle\s*\{\s*display:\s*block;/s)
    expect(conversationStyles).not.toContain('.interaction-card-resize-handle:hover::after')
    expect(conversationStyles).not.toMatch(/\.is-resizing[\s\S]*> \.interaction-card-resize-handle::after/s)
    expect(conversationStyles).toMatch(/\.interaction-card-resize-handle:focus-visible::after\s*\{\s*opacity:\s*1;/s)
    expect(conversationStyles).toMatch(/@media \(forced-colors:\s*active\)[\s\S]*\.interaction-card-resize-handle:focus-visible::after\s*\{[^}]*background:\s*Highlight;/s)
    expect(conversationStyles).toMatch(/@media \(forced-colors:\s*active\)[\s\S]*:is\(\.approval-composer, \.plan-question-composer, \.plan-review-composer\)\s*\{[^}]*outline:\s*1px solid ButtonText;/s)
    expect(conversationStyles).toMatch(/@media \(prefers-reduced-motion:\s*reduce\)[\s\S]*\.interaction-card-resize-handle::after,[\s\S]*transition:\s*none;/s)
    for (const selector of [
      '.approval-composer-body',
      '.approval-composer-footer',
      '.plan-review-composer-body',
      '.plan-review-rejection-form',
      '.plan-review-composer-footer',
      '.plan-question-composer-body',
      '.plan-question-composer-footer',
    ]) {
      const rule = conversationStyles.match(new RegExp(
        `${selector.replaceAll('.', '\\.')}\\s*\\{[^}]*\\}`,
        's',
      ))?.[0] ?? ''
      expect(rule).not.toBe('')
      expect(rule).not.toMatch(/border-(?:top|bottom|block)|border:/)
    }
  })

  it('品牌组合保持既定高度、比例和深色主题字标', () => {
    const uiStyles = cssFiles['../components/ui/ui.css']
    const workspaceStyles = cssFiles['../features/workspace/workspace.css']

    expect(workspaceStyles).toMatch(/\.empty-brand-lockup\s*\{[^}]*display:\s*grid;[^}]*place-items:\s*center;/s)
    expect(workspaceStyles).not.toMatch(/\.brand-(?:name|plus)|\.empty-brand-name/)
    expect(uiStyles).toMatch(/\.brand-logo\s*\{[^}]*--brand-logo-height:\s*22px;[^}]*height:\s*var\(--brand-logo-height\);[^}]*aspect-ratio:\s*2010 \/ 458;/s)
    expect(uiStyles).toContain('.brand-logo--md { --brand-logo-height: 30px; }')
    expect(uiStyles).toContain('.brand-logo--lg { --brand-logo-height: 42px; }')
    expect(uiStyles).toMatch(/:root\[data-theme='dark'\] \.brand-logo__wordmark\s*\{[^}]*filter:\s*brightness\(0\) invert\(1\);/s)
    expect(uiStyles).toMatch(/@media \(max-width:\s*440px\)[\s\S]*\.brand-logo--lg\s*\{[^}]*--brand-logo-height:\s*38px;/s)
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

  it('文本选区只由全局入口消费独立的高对比语义色', () => {
    const globalStyles = cssFiles['./global.css']
    const selectionOwners = Object.entries(cssFiles)
      .filter(([, styles]) => styles.includes('::selection'))
      .map(([path]) => path)

    expect(selectionOwners).toEqual(['./global.css'])
    expect(tokensStyles).toContain('--color-selection: var(--primitive-blue-300);')
    expect(tokensStyles).toContain('--color-on-selection: var(--primitive-gray-950);')
    expect(tokensStyles).toMatch(/:root\[data-theme='dark'\]\s*\{[^}]*--color-selection:\s*var\(--color-brand\);[^}]*--color-on-selection:\s*var\(--color-on-brand\);/s)
    expect(globalStyles).toMatch(/::selection\s*\{[^}]*background:\s*var\(--color-selection\);[^}]*color:\s*var\(--color-on-selection\);/s)
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

  it('任务轨迹抽屉复用语义令牌并覆盖响应式与辅助模式', () => {
    const conversationStyles = cssFiles['../features/conversation/conversation.css']
    const todoTraceStyles = cssFiles['../features/conversation/todoTrace/todoTrace.css']
    const drawerRegion = todoTraceStyles.match(/\.todo-trace-drawer-region\s*\{([^}]*)\}/s)?.[1] ?? ''

    expect(tokensStyles).toContain('--layout-drawer-width: 400px;')
    expect(todoTraceStyles).toMatch(/\.todo-trace-drawer\s*\{[^}]*width:\s*min\(var\(--layout-drawer-width\), 100vw\);/s)
    expect(todoTraceStyles).toMatch(/@media \(min-width: 1281px\)[\s\S]*\.app-shell\.has-todo-trace \.workspace-main\s*\{[^}]*margin-right:\s*var\(--layout-drawer-width\);/s)
    expect(todoTraceStyles).toMatch(/@media \(max-width: 440px\)[\s\S]*\.todo-trace-drawer\s*\{[^}]*width:\s*100vw;/s)
    expect(conversationStyles).toMatch(/@media \(any-hover: none\), \(any-pointer: coarse\)[\s\S]*\.composer-auxiliary-control\s*\{[^}]*min-height:\s*var\(--control-lg\);/s)
    expect(todoTraceStyles).toMatch(/@media \(forced-colors: active\)[\s\S]*\.todo-trace-todo::before\s*\{[^}]*background:\s*ButtonText;/s)
    expect(todoTraceStyles).toMatch(/@media \(prefers-reduced-motion: reduce\)[\s\S]*\.todo-trace-spin\s*\{[^}]*animation:\s*none;/s)
    expect(todoTraceStyles).toMatch(/\.todo-trace-update-body\s*\{[^}]*padding:\s*var\(--space-1-5\) var\(--space-3\) 0;/s)
    expect(todoTraceStyles).toMatch(/\.todo-trace-update-body \.todo-trace-todo-list\s*\{[^}]*padding-bottom:\s*0;/s)
    expect(todoTraceStyles).toMatch(/\.todo-trace-update-body \.todo-trace-todo-list::before\s*\{[^}]*bottom:\s*calc\(var\(--control-sm\) \/ 2\);/s)
    expect(drawerRegion).not.toMatch(/\bborder(?:-radius)?\s*:/)
    expect(drawerRegion).not.toMatch(/\bbackground\s*:/)
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

  it('共享与业务按钮都声明完整交互状态', () => {
    const contracts = [
      [cssFiles['../components/ui/ui.css'], '.toast-card > button'],
      [cssFiles['../features/workspace/workspace.css'], '.conversation-main'],
      [cssFiles['../features/workspace/workspace.css'], '.user-card'],
      [cssFiles['../components/ui/ui.css'], '.ui-compact-picker-trigger'],
      [cssFiles['../features/conversation/conversation.css'], '.composer-auxiliary-control'],
    ] as const

    for (const [source, selector] of contracts) {
      for (const state of ['hover', 'active', 'focus-visible', 'disabled']) {
        expect(source, `${selector} 缺少 ${state}`).toContain(`${selector}:${state}`)
      }
    }
  })

  it('Markdown 使用编辑型表格并具备完整文章语义和显式 compact variant', () => {
    const markdownStyles = cssFiles['../features/conversation/conversation.css']
    expect(markdownStyles).toMatch(/\.markdown-content h1[\s\S]*\.markdown-content h6/)
    expect(markdownStyles).toMatch(/\.markdown-content--article p\s*\{[^}]*margin:\s*var\(--space-4\) 0 var\(--space-1\);/s)
    expect(markdownStyles).toMatch(/\.markdown-content li > ul[\s\S]*padding-inline-start/)
    expect(markdownStyles).toMatch(/\.markdown-table-wrap[\s\S]*overflow-x:\s*auto/)
    expect(markdownStyles).toMatch(/\.markdown-content th,[\s\S]*border-bottom:\s*1px solid var\(--color-border\)/)
    expect(markdownStyles).toContain('.markdown-content--compact')
    expect(markdownStyles).toMatch(/\.markdown-content \.markdown-bare-url\s*\{[^}]*word-break:\s*break-all;/s)
    expect(markdownStyles).not.toMatch(/tbody tr:nth-child|tbody tr:hover/)
  })

  it('三态侧栏、头部 search 与必要断点均由 workspace 所有', () => {
    const workspaceStyles = cssFiles['../features/workspace/workspace.css']
    expect(workspaceStyles).toMatch(/grid-template-columns:\s*var\(--layout-sidebar-expanded\)/)
    expect(workspaceStyles).toMatch(/data-sidebar-mode='rail'[\s\S]*var\(--layout-sidebar-rail\)/)
    expect(workspaceStyles).toContain('.sidebar-head.is-search-open')
    expect(workspaceStyles).toMatch(/\.workspace-sidebar\s*\{[^}]*overflow:\s*visible;/s)
    expect(workspaceStyles).toMatch(/\.sidebar-rail\s*\{[^}]*--sidebar-rail-control-size:\s*var\(--control-sm\);[^}]*gap:\s*calc\(var\(--space-4\) \+ var\(--space-0-5\)\);[^}]*padding:\s*calc\(\(var\(--layout-header-height\) - var\(--sidebar-rail-control-size\)\) \/ 2\) var\(--space-1-5\) var\(--space-3\);/s)
    expect(workspaceStyles).toMatch(/\.sidebar-rail \.ui-icon-button\s*\{[^}]*width:\s*var\(--control-sm\);[^}]*min-width:\s*var\(--control-sm\);[^}]*min-height:\s*var\(--control-sm\);[^}]*height:\s*var\(--control-sm\);/s)
    expect(workspaceStyles).toMatch(/\.sidebar-head\s*\{[^}]*align-items:\s*center;[^}]*gap:\s*var\(--space-2\);[^}]*min-height:\s*var\(--layout-header-height\);[^}]*margin-bottom:\s*var\(--space-1\);/s)
    expect(workspaceStyles).toMatch(/\.sidebar-head-actions\s*\{[^}]*margin-left:\s*auto;[^}]*flex:\s*0 0 auto;[^}]*align-items:\s*center;[^}]*gap:\s*var\(--space-1\);/s)
    expect(workspaceStyles).toMatch(/\.brand\s*\{[^}]*overflow:\s*hidden;[^}]*flex:\s*1 1 auto;[^}]*padding:\s*0;/s)
    expect(workspaceStyles).not.toContain('.brand > span')
    expect(workspaceStyles).not.toMatch(/\.brand-(?:name|plus)|\.empty-brand-name/)
    expect(workspaceStyles).toMatch(/\.sidebar-head-actions \.ui-icon-button\s*\{[^}]*width:\s*var\(--control-xs\);[^}]*min-height:\s*var\(--control-xs\);[^}]*height:\s*var\(--control-xs\);/s)
    expect(workspaceStyles).toMatch(/@media \(any-hover:\s*none\), \(any-pointer:\s*coarse\)[\s\S]*\.sidebar-head-actions \.ui-icon-button\s*\{[^}]*width:\s*var\(--control-lg\);[^}]*min-width:\s*var\(--control-lg\);[^}]*height:\s*var\(--control-lg\);/s)
    expect(workspaceStyles).toMatch(/@media \(any-hover:\s*none\), \(any-pointer:\s*coarse\)[\s\S]*\.sidebar-rail\s*\{[^}]*--sidebar-rail-control-size:\s*var\(--control-lg\);/s)
    expect(workspaceStyles).toMatch(/@media \(any-hover:\s*none\), \(any-pointer:\s*coarse\)[\s\S]*\.sidebar-rail \.ui-icon-button\s*\{[^}]*width:\s*var\(--control-lg\);[^}]*min-width:\s*var\(--control-lg\);[^}]*min-height:\s*var\(--control-lg\);[^}]*height:\s*var\(--control-lg\);/s)
    expect(workspaceStyles).toMatch(/@media \(any-hover:\s*none\), \(any-pointer:\s*coarse\)[\s\S]*\.sidebar-head\s*\{[^}]*gap:\s*var\(--space-1\);[^}]*\}[\s\S]*\.sidebar-head-actions\s*\{[^}]*gap:\s*var\(--space-0\);/s)
    expect(workspaceStyles).toMatch(/\.brand:hover,[\s\S]*\.brand:active\s*{[^}]*background:\s*transparent;[^}]*box-shadow:\s*none;[^}]*color:\s*var\(--color-text-primary\);/s)
    expect(workspaceStyles).toMatch(/\.sidebar-head-actions \.ui-tooltip\s*\{[^}]*right:\s*0;[^}]*left:\s*auto;/s)
    expect(workspaceStyles).toMatch(/\.sidebar-head-actions \.sidebar-mode-toggle \+ \.ui-tooltip\s*\{[^}]*top:\s*50%;[^}]*right:\s*auto;[^}]*left:\s*calc\(100% \+ var\(--space-3\)\);[^}]*transform:\s*translate\(var\(--space-0-5\), -50%\);/s)
    expect(workspaceStyles).toMatch(/\.sidebar-head-actions \.ui-icon-button-wrap:hover \.sidebar-mode-toggle \+ \.ui-tooltip,[\s\S]*\.sidebar-head-actions \.sidebar-mode-toggle:focus-visible \+ \.ui-tooltip\s*\{[^}]*transform:\s*translate\(0, -50%\);/s)
    expect(workspaceStyles).toMatch(/\.sidebar-rail \.ui-tooltip\s*\{[^}]*top:\s*50%;[^}]*left:\s*calc\(100% \+ var\(--space-5\)\);[^}]*transform:\s*translate\(var\(--space-0-5\), -50%\);/s)
    expect(workspaceStyles).toMatch(/\.sidebar-rail \.ui-icon-button-wrap:hover \.ui-tooltip,[\s\S]*\.sidebar-rail \.ui-icon-button:focus-visible \+ \.ui-tooltip\s*\{[^}]*transform:\s*translate\(0, -50%\);/s)
    expect(workspaceStyles).toMatch(/@media \(max-width:\s*767px\)/)
    expect(workspaceStyles).not.toMatch(/\.app-shell\s*\{[^}]*transition:\s*grid-template-columns/s)
    expect(workspaceStyles).not.toMatch(/\.workspace-main\s*\{[^}]*transition:\s*margin-right/s)
    expect(workspaceStyles).toMatch(/\[data-workspace-layout-target\]\.is-layout-flipping\s*\{[^}]*will-change:\s*transform, opacity;/s)
    expect(workspaceLayoutAnimation).toContain('Flip.getState')
    expect(workspaceLayoutAnimation).toContain('Flip.from')
    expect(workspaceLayoutAnimation).toContain("'(prefers-reduced-motion: reduce)'")
    expect(workspaceLayoutAnimation).toContain('Flip.killFlipsOf')
    expect(workspaceStyles).toMatch(/@media \(prefers-reduced-motion:\s*reduce\)[\s\S]*\.workspace-main,[\s\S]*transition:\s*none;/s)
    expect(cssFiles['../components/ui/ui.css']).not.toContain('.ui-scrollbar-overlay')
    expect(cssFiles['../components/ui/ui.css']).toContain('.ui-overlay-scrollbar')
  })

  it('回到底部与任务轨迹共享输入框上方的布局和按钮视觉契约', () => {
    const conversationStyles = cssFiles['../features/conversation/conversation.css']
    const workspaceStyles = cssFiles['../features/workspace/workspace.css']
    const auxiliaryControl = conversationStyles.match(/\.composer-auxiliary-control\s*\{([^}]*)\}/s)?.[1] ?? ''
    expect(workspaceStyles).not.toContain('.conversation-scroll-action')
    expect(workspaceStyles).toMatch(
      /\.message-list\s*\{[^}]*padding:[^;}]*var\(--composer-height\);/s,
    )
    expect(conversationStyles).toMatch(
      /\.composer-auxiliary-controls\s*\{[^}]*display:\s*grid;[^}]*grid-template-columns:\s*minmax\(0, 1fr\) auto minmax\(0, 1fr\);[^}]*width:\s*min\(100%, var\(--layout-composer-width\)\);[^}]*margin:\s*0 auto var\(--space-1-5\);[^}]*align-items:\s*center;/s,
    )
    expect(conversationStyles).not.toMatch(/\.composer-auxiliary-controls\s*\{[^}]*position:\s*absolute;/s)
    expect(conversationStyles).toMatch(/\.composer-dock\.is-hero \.composer-note\s*\{[^}]*position:\s*static;[^}]*transform:\s*none;[^}]*margin-top:\s*var\(--space-3\);/s)
    expect(conversationStyles).toMatch(/\.composer-scroll-to-bottom-control\s*\{[^}]*grid-column:\s*2;[^}]*justify-self:\s*center;/s)
    expect(conversationStyles).toMatch(/\.composer-task-trace-control\s*\{[^}]*grid-column:\s*3;[^}]*justify-self:\s*end;/s)
    expect(conversationStyles).toMatch(/@media \(max-width: 440px\)[\s\S]*\.composer-auxiliary-controls\s*\{[^}]*gap:\s*var\(--space-4\);[^}]*\}[\s\S]*\.composer-auxiliary-control\s*\{[^}]*padding-inline:\s*var\(--space-1-5\);/s)
    expect(conversationStyles).toMatch(/@media \(max-width: 1023px\)[\s\S]*\.composer-auxiliary-controls,\s*\.composer,\s*\.composer-note\s*\{[^}]*max-width:\s*var\(--layout-content-medium\);/s)
    expect(auxiliaryControl).toContain('gap: var(--space-1-5);')
    expect(auxiliaryControl).toContain('min-height: var(--control-sm);')
    expect(auxiliaryControl).toContain('padding: 0 var(--space-3);')
    expect(auxiliaryControl).toContain('border-radius: var(--radius-pill);')
    expect(auxiliaryControl).toContain('font-size: var(--type-caption-size);')
    expect(auxiliaryControl).toContain('font-weight: var(--weight-medium);')
    expect(auxiliaryControl).toContain('line-height: var(--type-caption-line);')
    expect(workspaceStyles).toMatch(/\.scroll-to-bottom\s*\{[^}]*opacity:\s*0;[^}]*transition:[^}]*opacity var\(--motion-slow\)/s)
    expect(workspaceStyles).toMatch(/\.scroll-to-bottom\.is-fading\s*\{[^}]*transform:\s*translateY\(var\(--space-2\)\);[^}]*opacity:\s*0;[^}]*pointer-events:\s*none;/s)
    expect(conversationStyles).toMatch(/\.composer-auxiliary-control\.scroll-to-bottom\s*\{[^}]*border:\s*0;/s)
    expect(workspaceStyles).not.toContain('.scroll-to-bottom::before')
  })

  it('输入区通过渐隐层衔接滚动内容，侧栏保留独立的历史滚动区和账户区', () => {
    const conversationStyles = cssFiles['../features/conversation/conversation.css']
    const workspaceStyles = cssFiles['../features/workspace/workspace.css']
    const uiStyles = cssFiles['../components/ui/ui.css']

    expect(conversationStyles).not.toContain('.composer-wrap')
    expect(conversationStyles).toMatch(/\.tool-row\.running > summary::after,\s*\.tool-row\.paused > summary::after,\s*\.subagent-card\.running > summary::after,\s*\.subagent-card\.paused > summary::after\s*\{[^}]*left:\s*0;[^}]*width:\s*300px;[^}]*color-mix\(in srgb, var\(--color-canvas\) 60%, transparent\) 55%,[^}]*animation:\s*conversation-tool-row-sweep var\(--motion-tool-sweep-cycle\) ease-out infinite;/s)
    expect(conversationStyles).toMatch(/@keyframes conversation-tool-row-sweep\s*\{[\s\S]*?0%\s*\{[^}]*left:\s*-300px;[\s\S]*?90%, 100%\s*\{[^}]*left:\s*100%;/s)
    expect(conversationStyles).toMatch(/@media \(prefers-reduced-motion: reduce\)[\s\S]*\.tool-row\.running > summary::after,\s*\.tool-row\.paused > summary::after,\s*\.subagent-card\.running > summary::after,\s*\.subagent-card\.paused > summary::after,[\s\S]*animation:\s*none;/s)
    expect(conversationStyles).toMatch(/\.tool-field-pending\s*\{[^}]*width:\s*var\(--space-12\);[^}]*overflow:\s*hidden;/s)
    expect(conversationStyles).toMatch(/@keyframes conversation-tool-result-pulse\s*\{[\s\S]*?0%\s*\{[^}]*opacity:\s*0;[\s\S]*?100%\s*\{[^}]*translate\(calc\(var\(--space-12\) \+ 10px\), -50%\);[^}]*opacity:\s*0;/s)
    expect(workspaceStyles).toMatch(/\.message-list\s*\{[^}]*--conversation-flow-gap:\s*var\(--space-4\);/s)
    expect(workspaceStyles).not.toContain('--conversation-flow-gap-surface')
    expect(workspaceStyles).not.toContain('--conversation-flow-gap-mixed')
    expect(workspaceStyles).not.toContain('--conversation-flow-gap-content')
    expect(workspaceStyles).toMatch(/\.message-list > :is\([\s\S]*?\.approval-wait-state,[\s\S]*?\.plan-interaction-wait-state,[\s\S]*?\) \+ :is\([\s\S]*?\.approval-wait-state,[\s\S]*?\.plan-interaction-wait-state,[\s\S]*?\)\s*\{[^}]*margin-top:\s*var\(--conversation-flow-gap\);/s)
    expect(workspaceStyles).toMatch(/\.message-list > \.user-message \+ :is\(\.assistant-message, \.conversation-run-failure\)\s*\{[^}]*margin-top:\s*0;/s)
    expect(workspaceStyles).toMatch(/\.message-list > :is\(\.assistant-message:has\(> \.message-action-row--assistant\), \.conversation-run-failure\) \+ \.user-message\s*\{[^}]*margin-top:\s*var\(--space-12\);/s)
    expect(workspaceStyles).toMatch(/\.message-list > :is\([\s\S]*?\.message-history-loader,[\s\S]*?\.message-stream-tail[\s\S]*?\) \+ :is\([\s\S]*?\.message-history-loader,[\s\S]*?\.message-stream-tail/s)
    expect(workspaceStyles).toMatch(/\.message-history-loader\s*\{[^}]*display:\s*grid;[^}]*place-items:\s*center;[^}]*\}/s)
    expect(workspaceStyles).not.toMatch(/\.message-history-loader\s*\{[^}]*padding-bottom:/s)
    expect(conversationStyles).toMatch(/\.message-stream-tail\s*\{[^}]*margin:\s*0;/s)
    expect(conversationStyles).not.toContain('.streaming-indicator')
    expect(conversationStyles).not.toContain(':has(+ .approval-wait-state)')
    expect(tokensStyles).toContain('--optical-activity-dots-inset: 1px;')
    expect(conversationStyles).toMatch(/\.activity-dots\s*\{[^}]*margin-inline-start:\s*var\(--optical-activity-dots-inset\);/s)
    expect(conversationStyles).toMatch(/\.approval-wait-state > \.activity-dots,\s*\.plan-interaction-wait-state > \.activity-dots\s*\{[^}]*color:\s*var\(--color-text-tertiary\);/s)
    expect(conversationStyles).toMatch(/\.composer-dock\s*\{[^}]*grid-row:\s*2;[^}]*grid-column:\s*1;[^}]*align-self:\s*end;[^}]*pointer-events:\s*none;[^}]*background:\s*transparent;/s)
    expect(conversationStyles).toMatch(/\.composer-dock::before\s*\{[^}]*height:\s*calc\(100% \+ var\(--space-16\)\);[^}]*linear-gradient\(to bottom, transparent, var\(--color-canvas\)\);/s)
    expect(conversationStyles).toMatch(/\.composer-dock\.is-hero\s*\{[^}]*align-self:\s*center;[^}]*display:\s*flex;[^}]*padding-bottom:\s*0;[^}]*flex-direction:\s*column;/s)
    expect(conversationStyles).toMatch(/\.composer-dock\.is-hero::before\s*\{\s*display:\s*none;/s)
    expect(conversationStyles).toMatch(/\.composer-hero\s*\{[^}]*width:\s*min\(100%, var\(--layout-composer-width\)\);[^}]*margin:\s*0 auto var\(--type-composer-line\);[^}]*place-items:\s*center;/s)
    expect(conversationStyles).toMatch(/\.composer-input-grow,[\s\S]*\.composer-input-mirror\s*\{[^}]*min-height:\s*calc\(\(var\(--type-composer-line\) \* 2\) \+ var\(--space-1\)\);/s)
    expect(conversationStyles).not.toMatch(/\.composer-dock\.is-hero \.composer-input-mirror\s*\{/s)
    expect(workspaceStyles).toMatch(/\.sidebar-wide\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\);[^}]*grid-template-rows:\s*auto auto minmax\(0, 1fr\) auto;/s)
    expect(workspaceStyles).toMatch(/\.sidebar-head\s*\{[^}]*z-index:\s*var\(--layer-dropdown\);[^}]*overflow:\s*visible;/s)
    expect(workspaceStyles).toMatch(/\.new-chat\s*\{[^}]*min-height:\s*var\(--control-lg\);[^}]*justify-content:\s*center;[^}]*box-shadow:\s*var\(--shadow-1\);/s)
    expect(workspaceStyles).toMatch(/\.new-chat-wrap\s*\{[^}]*margin-bottom:\s*var\(--space-3\);/s)
    expect(workspaceStyles).toMatch(/\.new-chat-shortcut\s*\{[^}]*color:\s*inherit;/s)
    expect(workspaceStyles).toMatch(/\.primary-nav \.ui-button\s*\{[^}]*font-size:\s*var\(--type-ui-size\);[^}]*font-weight:\s*var\(--weight-regular\);[^}]*line-height:\s*var\(--type-ui-line\);/s)
    expect(workspaceStyles).toMatch(/\.primary-nav\s*\{[^}]*padding-bottom:\s*var\(--space-4\);[^}]*border:\s*0;/s)
    expect(workspaceStyles).toContain('.new-chat:hover .new-chat-shortcut')
    expect(workspaceStyles).toMatch(/\.conversation-history::after\s*\{[^}]*position:\s*absolute;[^}]*bottom:\s*0;[^}]*height:\s*var\(--space-6\);[^}]*linear-gradient\(to bottom, transparent, var\(--color-sidebar\)\);[^}]*pointer-events:\s*none;/s)
    expect(workspaceStyles).toMatch(/\.conversation-attention-dot\s*\{[^}]*transform-origin:\s*center;[^}]*animation:\s*workspace-attention-breathe var\(--motion-attention-cycle\)/s)
    expect(workspaceStyles).toMatch(/\.conversation-attention-dot\.is-approval\s*\{[^}]*color:\s*var\(--color-warning-panel-accent\);/s)
    expect(workspaceStyles).toMatch(/\.conversation-attention-dot\.is-plan\s*\{[^}]*color:\s*var\(--color-plan-panel-accent\);/s)
    expect(workspaceStyles).toMatch(/@keyframes workspace-attention-breathe\s*\{[\s\S]*?0%, 100%\s*\{[^}]*transform:\s*scale\(\.88\);[^}]*opacity:\s*\.7;[\s\S]*?50%\s*\{[^}]*transform:\s*scale\(1\);[^}]*opacity:\s*1;/s)
    expect(workspaceStyles).not.toContain('.history-toolbar')
    expect(workspaceStyles).toMatch(/\.conversation-sticky-title\s*\{[^}]*position:\s*absolute;[^}]*top:\s*0;[^}]*right:\s*0;[^}]*left:\s*0;[^}]*height:\s*22px;/s)
    expect(workspaceStyles).toMatch(/\.conversation-group-title,[\s\S]*\.conversation-sticky-title\s*\{[^}]*font-size:\s*var\(--type-caption-size\);/s)
    expect(workspaceStyles).toMatch(/\.conversation-item\s*\{[^}]*min-height:\s*var\(--control-md\);[^}]*margin:\s*0;/s)
    expect(workspaceStyles).toMatch(/\.conversation-main\s*\{[^}]*padding:\s*0 10px;/s)
    expect(workspaceStyles).toMatch(/\.conversation-item:hover \.conversation-main,[\s\S]*\.conversation-item:has\(:focus-visible\) \.conversation-main,[\s\S]*\.conversation-item:has\(\.conversation-more\.is-selected\) \.conversation-main,[\s\S]*\.conversation-item\.is-active \.conversation-main\s*\{[^}]*padding-right:\s*var\(--control-sm\);/s)
    expect(workspaceStyles).toMatch(/\.conversation-title-marquee \.overflow-marquee-content\s*\{[^}]*font-size:\s*var\(--type-meta-size\);[^}]*font-weight:\s*var\(--weight-regular\);[^}]*line-height:\s*var\(--type-meta-line\);/s)
    expect(workspaceStyles).toMatch(/\.conversation-item\.is-active \.conversation-title-marquee \.overflow-marquee-content\s*\{[^}]*font-weight:\s*var\(--weight-medium\);/s)
    expect(workspaceStyles).toMatch(/\.conversation-item:hover \.conversation-title-marquee\.is-overflowing,[\s\S]*\.conversation-item:has\(:focus-visible\) \.conversation-title-marquee\.is-overflowing,[\s\S]*\.conversation-item:has\(\.conversation-more\.is-selected\) \.conversation-title-marquee\.is-overflowing,[\s\S]*\.conversation-item\.is-active \.conversation-title-marquee\.is-overflowing\s*\{[^}]*-webkit-mask-image:\s*linear-gradient\(to right, black calc\(100% - var\(--space-3\)\), transparent calc\(100% - var\(--space-1\)\)\);[^}]*mask-image:\s*linear-gradient\(to right, black calc\(100% - var\(--space-3\)\), transparent calc\(100% - var\(--space-1\)\)\);/s)
    expect(workspaceStyles).not.toContain('.conversation-more::before')
    expect(workspaceStyles).not.toContain('.conversation-item:focus-within')
    expect(workspaceStyles).toMatch(/\.conversation-item:has\(:focus-visible\) \.conversation-more,[\s\S]*\.conversation-more\.is-selected,[\s\S]*\.conversation-item\.is-active \.conversation-more\s*\{[^}]*opacity:\s*1;/s)
    expect(workspaceStyles).toMatch(/@media \(any-hover: none\), \(any-pointer: coarse\)[\s\S]*\.conversation-main\s*\{[^}]*padding-right:\s*calc\(var\(--control-lg\) \+ var\(--space-1\)\);/s)
    expect(workspaceStyles).toMatch(/\.conversation-scroll\s*\{[^}]*padding-bottom:\s*var\(--space-6\);/s)
    expect(workspaceStyles).toMatch(/\.conversation-history\s*\{[^}]*margin-right:\s*calc\(var\(--space-3\) \* -1\);/s)
    expect(workspaceStyles).toMatch(/\.conversation-scroll\s*\{[^}]*padding-right:\s*var\(--space-3\);/s)
    expect(workspaceStyles).not.toContain('--sidebar-scrollbar-thumb')
    expect(tokensStyles).toContain('--color-scroll-thumb: #e5e5e5;')
    expect(tokensStyles).toContain('--color-scroll-thumb-hover: #d4d4d4;')
    expect(tokensStyles).toContain('--color-scroll-thumb: #3c3c3d;')
    expect(tokensStyles).toContain('--color-scroll-thumb-hover: #545557;')
    expect(uiStyles).toMatch(/\.ui-scrollbar\s*\{[^}]*scrollbar-width:\s*none;/s)
    expect(uiStyles).toMatch(/\.ui-scrollbar::-webkit-scrollbar\s*\{[^}]*display:\s*none;/s)
    expect(uiStyles).toMatch(/\.ui-scrollbar:focus-visible\s*\{[^}]*outline:\s*0;/s)
    expect(uiStyles).toMatch(/@media \(forced-colors:\s*active\)[\s\S]*\.ui-scrollbar:focus-visible\s*\{[^}]*outline:\s*2px solid Highlight;[^}]*outline-offset:\s*-2px;/s)
    expect(uiStyles).toMatch(/\.ui-overlay-scrollbar__thumb::after\s*\{[^}]*border-radius:\s*var\(--radius-pill\);[^}]*background:\s*var\(--color-scroll-thumb\);[^}]*transition:[^}]*width var\(--motion-fast\) var\(--ease-out\)[,;]/s)
    expect(uiStyles).toMatch(/\.ui-overlay-scrollbar--vertical \.ui-overlay-scrollbar__thumb:hover::after,[\s\S]*\.ui-overlay-scrollbar--vertical \.ui-overlay-scrollbar__thumb:active::after\s*\{[^}]*width:\s*calc\(100% \+ var\(--space-0-5\)\);[^}]*background:\s*var\(--color-scroll-thumb-hover\);/s)
    expect(uiStyles).not.toContain('is-geometry-transitioning')
    expect(workspaceStyles).toMatch(/\.conversation-scroll\s*\{[^}]*overflow-anchor:\s*none;/s)
    expect(workspaceStyles).toMatch(/\.history-load-sentinel\s*\{[^}]*height:\s*1px;[^}]*overflow:\s*hidden;/s)
    expect(workspaceStyles).toMatch(/\.history-pagination-slot\s*\{[^}]*height:\s*var\(--control-lg\);/s)
    expect(workspaceStyles).toMatch(/\.history-pagination-status\s*\{[^}]*height:\s*100%;[^}]*padding:\s*0 var\(--space-2\);/s)
    expect(workspaceStyles).toMatch(/@media \(forced-colors:\s*active\)[\s\S]*\.conversation-history::after\s*{\s*display:\s*none;/s)
    expect(workspaceStyles).toMatch(/\.user-account\s*\{[^}]*min-height:\s*var\(--space-16\);[^}]*border:\s*0;/s)
    expect(uiStyles).toMatch(/\.user-avatar--sm\s*\{[^}]*width:\s*var\(--control-xs\);[^}]*height:\s*var\(--control-xs\);/s)
    expect(workspaceStyles).toMatch(/\.conversation-item\.is-active \.conversation-more\s*\{[^}]*opacity:\s*1;/s)
    expect(workspaceStyles).toMatch(/@media \(any-hover: none\), \(any-pointer: coarse\)[\s\S]*\.conversation-main,[\s\S]*min-height:\s*var\(--control-lg\);/s)
  })

  it('图标按钮全透明无阴影，标准输入框统一描边，Composer 保留独立弱边框', () => {
    const uiStyles = cssFiles['../components/ui/ui.css']
    const workspaceStyles = cssFiles['../features/workspace/workspace.css']
    const conversationStyles = cssFiles['../features/conversation/conversation.css']

    expect(uiStyles).not.toContain('.ui-icon-button::before')
    expect(uiStyles).toMatch(/\.ui-icon-button\s*\{[^}]*background:\s*transparent;/s)
    expect(uiStyles).toMatch(/\.ui-icon-button:hover:not\(:disabled\),[\s\S]*\.ui-icon-button:disabled\s*\{[^}]*background:\s*transparent;[^}]*box-shadow:\s*none;/s)
    expect(uiStyles).toMatch(/\.ui-icon-button:focus-visible\s*\{[^}]*outline:\s*0;[^}]*background:\s*var\(--color-hover\);[^}]*box-shadow:\s*none;/s)
    expect(uiStyles).toMatch(/@media \(forced-colors:\s*active\)[\s\S]*\.ui-button:focus-visible,[\s\S]*\.ui-temporal-picker__trigger:focus-visible,[\s\S]*\.ui-icon-button:focus-visible,[\s\S]*\.ui-text-field__control:focus-within\s*\{[^}]*outline:\s*2px solid Highlight;[^}]*outline-offset:\s*2px;/s)
    expect(uiStyles).toMatch(/\.ui-text-field__control,\s*\.ui-temporal-picker__trigger\s*\{[^}]*border:\s*1px solid var\(--color-border\);[^}]*background:\s*var\(--color-layer-1\);/s)
    expect(uiStyles).toMatch(/\.ui-text-field__control:focus-within,\s*\.ui-temporal-picker__trigger:focus-visible\s*\{[^}]*border-color:\s*var\(--color-border-strong\);[^}]*box-shadow:\s*none;/s)
    expect(workspaceStyles).toMatch(/\.sidebar-search\s*\{[^}]*top:\s*50%;[^}]*right:\s*0;[^}]*left:\s*0;[^}]*height:\s*var\(--control-lg\);[^}]*transform:\s*translateY\(-50%\) scaleX\(\.28\);/s)
    expect(workspaceStyles).toMatch(/\.sidebar-head\.is-search-open \.sidebar-search\s*\{[^}]*transform:\s*translateY\(-50%\) scaleX\(1\);/s)
    expect(workspaceStyles).toMatch(/\.sidebar-search:focus-within\s*\{[^}]*border:\s*0;[^}]*outline:\s*0;[^}]*box-shadow:\s*none;/s)
    expect(workspaceStyles).toMatch(/@media \(forced-colors:\s*active\)[\s\S]*\.sidebar-search:focus-within\s*\{[^}]*outline:\s*2px solid Highlight;[^}]*outline-offset:\s*-2px;/s)
    expect(conversationStyles).toMatch(/\.composer\s*\{[^}]*border:\s*1px solid var\(--color-border\);/s)
    expect(conversationStyles).toMatch(/\.composer:focus-within\s*\{[^}]*border-color:\s*var\(--color-border-strong\);[^}]*box-shadow:\s*var\(--shadow-2\)/s)
    expect(cssFiles['../features/conversation/attachments/attachments.css']).toMatch(/\.composer-attachment \.ui-button:focus-visible\s*\{[^}]*outline:\s*0;/s)
    expect(conversationStyles).not.toMatch(/\.message-list :is\([^}]*\.subagent-card[^}]*\)/s)
    expect(conversationStyles).toMatch(/\.tool-row-title\s*\{[^}]*font-weight:\s*var\(--weight-regular\)/s)
  })

  it('Header 只保留全局操作，模型选择归属 Composer 工具行', () => {
    const workspaceStyles = cssFiles['../features/workspace/workspace.css']
    const conversationStyles = cssFiles['../features/conversation/conversation.css']
    expect(workspaceStyles).not.toContain('.model-picker')
    expect(workspaceStyles).not.toContain('.agent-preset-picker')
    expect(cssFiles['../components/ui/ui.css']).toMatch(/\.ui-compact-picker\s*\{[^}]*width:\s*max-content;[^}]*max-width:\s*min\(220px, 45cqw\)/s)
    expect(conversationStyles).toMatch(/\.composer-toolbar\s*\{[^}]*justify-content:\s*space-between/s)
    expect(workspaceStyles).toMatch(
      /\.drawer-toggle \.ui-button__label\s*\{[^}]*display:\s*inline-flex;[^}]*white-space:\s*nowrap;/s,
    )
    expect(workspaceStyles).toMatch(
      /\.drawer-count\s*\{[^}]*display:\s*inline-grid;[^}]*place-items:\s*center;[^}]*background:\s*var\(--color-brand-soft\);[^}]*color:\s*var\(--color-brand-text\);[^}]*font-variant-numeric:\s*tabular-nums;/s,
    )
    expect(workspaceStyles).toMatch(
      /\.header-actions \.theme-switcher-circle,[\s\S]*\.drawer-toggle:focus-visible\s*{[^}]*border-color:\s*transparent;[^}]*background:\s*transparent;[^}]*box-shadow:\s*none;/s,
    )
    expect(workspaceStyles).toMatch(/\.workspace-title\s*\{[^}]*position:\s*absolute;[^}]*clip:\s*rect\(0, 0, 0, 0\);/s)
    expect(workspaceStyles).toMatch(/@media \(max-width:\s*767px\)[\s\S]*\.workspace-title\s*\{[^}]*position:\s*static;[^}]*flex:\s*1 1 auto;[^}]*font-size:\s*var\(--type-title-size\);[^}]*text-overflow:\s*ellipsis;/s)
  })

  it('大型内容表面共享同一视觉弧度，并只通过共享选中态呈现品牌强调', () => {
    const workspaceStyles = cssFiles['../features/workspace/workspace.css']
    const uiStyles = cssFiles['../components/ui/ui.css']
    const conversationStyles = cssFiles['../features/conversation/conversation.css']
    const settingsStyles = cssFiles['../features/settings/settings.css']

    expect(workspaceStyles).toMatch(/\.new-chat\s*\{[^}]*border-radius:\s*var\(--radius-lg\);[^}]*background:\s*var\(--color-layer-1\);/s)
    expect(conversationStyles).toMatch(/\.user-message \.message-markdown\s*\{[^}]*border-radius:\s*var\(--radius-3xl\);/s)
    expect(conversationStyles).toMatch(/\.user-message \.message-markdown\s*\{[^}]*word-break:\s*normal;[^}]*overflow-wrap:\s*break-word;/s)
    expect(conversationStyles).toMatch(/\.markdown-code-block\s*\{[^}]*border-radius:\s*var\(--radius-3xl\);/s)
    expect(conversationStyles).toMatch(/\.composer\s*\{[^}]*border-radius:\s*var\(--radius-3xl\);/s)
    expect(conversationStyles).toMatch(/\.approval-composer,[\s\S]*\.plan-review-composer\s*\{[^}]*border-radius:\s*var\(--radius-3xl\);/s)
    expect(uiStyles).toMatch(/\.ui-text-field__control\s*\{[^}]*border-radius:\s*var\(--radius-3xl\);/s)
    expect(uiStyles).toMatch(/\.ui-text-field--capsule \.ui-text-field__control\s*\{[^}]*border-radius:\s*var\(--radius-3xl\);/s)
    expect(uiStyles).toMatch(/\.modal-dialog\s*\{[^}]*border-radius:\s*var\(--radius-3xl\);/s)
    expect(conversationStyles).toMatch(/\.approval-rejection-form textarea,\s*\.plan-review-rejection-form textarea\s*\{[^}]*border-radius:\s*var\(--radius-3xl\);/s)
    expect(settingsStyles).toMatch(/@media \(max-width:\s*440px\)[\s\S]*\.settings-dialog\s*\{[^}]*border-radius:\s*var\(--radius-3xl\);/s)
    expect(conversationStyles).not.toMatch(/\.markdown-content--article \.markdown-code-block\s*\{[^}]*border-radius:/s)
    expect(uiStyles).toMatch(/\.ui-button\.is-selected\s*\{[^}]*background:\s*var\(--color-brand-soft\);/s)
    expect(uiStyles).toMatch(/\.ui-button\.is-selected:hover:not\(:disabled\)\s*\{[^}]*background:\s*var\(--color-brand-soft\);/s)
  })

  it('Composer 锁定命令菜单、附件条和单行工具控件的精修参数', () => {
    const conversationStyles = cssFiles['../features/conversation/conversation.css']

    expect(conversationStyles).toMatch(/\.composer\s*\{[^}]*gap:\s*var\(--space-3\);[^}]*padding:\s*10px 0 0;/s)
    expect(conversationStyles).toMatch(/\.composer-input-scroll\s*\{[^}]*max-height:\s*144px;[^}]*overflow-y:\s*auto;/s)
    expect(conversationStyles).toMatch(/\.composer-input,[\s\S]*\.composer-input-backdrop\s*\{[^}]*padding:\s*4px 12px 0 16px;[^}]*font-size:\s*var\(--type-body-size\);[^}]*line-height:\s*var\(--type-composer-line\);[^}]*white-space:\s*pre-wrap;/s)
    expect(conversationStyles).toMatch(/\.composer-toolbar\s*\{[^}]*align-items:\s*center;[^}]*min-height:\s*42px;[^}]*padding:\s*2px var\(--space-2\) 6px;/s)
    expect(conversationStyles).toMatch(/\.composer-add-button,[\s\S]*\.send-button\s*\{[^}]*height:\s*var\(--control-composer\);[^}]*min-height:\s*var\(--control-composer\);/s)
    expect(conversationStyles).toMatch(/\.composer-add-button\s*\{[^}]*border:\s*0;[^}]*background:\s*transparent;[^}]*box-shadow:\s*none;/s)
    expect(conversationStyles).toMatch(/\.composer-plan-chip\s*\{[^}]*gap:\s*var\(--space-1\);[^}]*min-width:\s*var\(--control-composer\);[^}]*height:\s*var\(--control-plan-chip\);[^}]*padding:\s*2px var\(--space-2\);[^}]*background:\s*var\(--color-warning-soft\);[^}]*font-size:\s*var\(--type-meta-size\);[^}]*line-height:\s*var\(--type-meta-line\);[^}]*box-shadow:\s*none;[^}]*transform:\s*none;/s)
    expect(conversationStyles).toMatch(/\.composer-plan-chip-close\s*\{[^}]*width:\s*var\(--space-3\);[^}]*height:\s*var\(--space-3\);/s)
    expect(conversationStyles).toMatch(/@media \(any-hover: none\), \(any-pointer: coarse\)[\s\S]*\.composer-plan-chip,[\s\S]*min-height:\s*var\(--control-lg\);[^}]*height:\s*var\(--control-lg\);/s)
    expect(conversationStyles).toMatch(/@media \(any-hover: none\), \(any-pointer: coarse\)[\s\S]*\.plan-question-option,[\s\S]*\.composer-suggestion-item\s*\{[^}]*min-height:\s*var\(--control-lg\);/s)
    expect(cssFiles['../features/conversation/attachments/attachments.css']).toMatch(/\.composer-attachment\s*\{[^}]*height:\s*var\(--control-md\);/s)
    expect(cssFiles['../features/conversation/attachments/attachments.css']).toMatch(/\.attachment-description\s*\{[^}]*min-height:\s*64px;[^}]*padding:\s*var\(--space-2\) var\(--space-4\);/s)
    expect(cssFiles['../features/conversation/attachments/attachments.css']).toMatch(/\.attachment-document-summary,[\s\S]*\.attachment-document-preview\s*\{[^}]*gap:\s*var\(--space-3\);/s)
    expect(cssFiles['../features/conversation/attachments/attachments.css']).toMatch(/\.attachment-description strong\s*\{[^}]*font-size:\s*var\(--type-meta-size\);/s)
    expect(cssFiles['../features/conversation/attachments/attachments.css']).toMatch(/body:has\(\.attachment-document-viewer\) \.attachment-card \.attachment-file-actions\s*\{[^}]*opacity:\s*0;[^}]*pointer-events:\s*none;/s)
    expect(cssFiles['../features/conversation/attachments/attachments.css']).toMatch(/body:not\(:has\(\.modal-backdrop\)\) \.attachment-description:not\(:hover\):has\(\.attachment-document-preview:focus\) \.attachment-file-actions\s*\{[^}]*opacity:\s*0;[^}]*pointer-events:\s*none;/s)
    expect(cssFiles['../features/conversation/attachments/attachments.css']).toMatch(/\.attachment-file-icon\s*\{[^}]*width:\s*var\(--control-lg\);[^}]*height:\s*var\(--control-xl\);[^}]*border-radius:\s*var\(--radius-lg\);/s)
    expect(cssFiles['../components/ui/ui.css']).toMatch(/\.ui-compact-picker-trigger\s*\{[^}]*height:\s*var\(--control-composer\);[^}]*padding-inline:\s*var\(--space-3\);[^}]*background:\s*transparent;[^}]*box-shadow:\s*none;/s)
    expect(cssFiles['../components/ui/ui.css']).toMatch(/\.ui-compact-picker-trigger\s*\{[^}]*border-radius:\s*var\(--radius-lg\);/s)
    expect(cssFiles['../components/ui/ui.css']).toMatch(/\.ui-compact-picker-trigger:hover:not\(:disabled\)\s*\{[^}]*box-shadow:\s*var\(--shadow-1\);/s)
    expect(cssFiles['../components/ui/ui.css']).toMatch(/\.ui-compact-picker-options\s*\{[^}]*width:\s*min\(250px,[^}]*padding:\s*var\(--space-2\);[^}]*border-radius:\s*var\(--radius-2xl\);[^}]*box-shadow:\s*var\(--shadow-2\);/s)
    expect(cssFiles['../components/ui/ui.css']).toMatch(/\.ui-compact-picker--access-mode \.ui-compact-picker-trigger\s*\{[^}]*justify-content:\s*flex-start;[^}]*gap:\s*var\(--space-2\);[^}]*padding-inline:\s*var\(--space-3\);[^}]*text-align:\s*left;/s)
    expect(cssFiles['../components/ui/ui.css']).toMatch(/\.ui-compact-picker--access-mode \.ui-compact-picker-chevron\s*\{[^}]*margin-left:\s*var\(--space-1\);/s)
    expect(cssFiles['../components/ui/ui.css']).toMatch(/\.ui-compact-picker-trigger\[aria-expanded='true'\]\s*\{[^}]*background:\s*transparent;[^}]*box-shadow:\s*none;/s)
    expect(cssFiles['../components/ui/ui.css']).toMatch(/\.ui-compact-picker--access-mode \.ui-compact-picker-options\s*\{[^}]*right:\s*auto;[^}]*left:\s*0;[^}]*width:\s*min\(calc\(var\(--space-16\) \* 2 \+ var\(--space-12\)\), calc\(100cqw - var\(--control-lg\) - var\(--space-2\)\)\);/s)
    expect(cssFiles['../components/ui/ui.css']).toMatch(/\.ui-compact-picker--access-mode \.ui-compact-option-label\s*\{[^}]*white-space:\s*nowrap;/s)
    expect(cssFiles['../components/ui/ui.css']).toMatch(/\.ui-compact-picker-trigger\[aria-expanded='true'\] \.ui-compact-picker-chevron\s*\{[^}]*transform:\s*rotate\(180deg\);/s)
    expect(cssFiles['../components/ui/ui.css']).toMatch(/\.ui-compact-picker-options \[role='option'\]\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\) var\(--icon-sm\);[^}]*min-height:\s*var\(--control-md\);[^}]*padding:\s*var\(--space-1\) var\(--space-3\);[^}]*border-radius:\s*var\(--radius-md\);[^}]*font-size:\s*var\(--type-ui-size\);[^}]*line-height:\s*var\(--type-ui-line\);[^}]*box-shadow:\s*none;/s)
    expect(cssFiles['../components/ui/ui.css']).toMatch(/\.ui-compact-picker-options \[role='option'\]\[aria-selected='true'\],[\s\S]*background:\s*transparent;[^}]*box-shadow:\s*none;/s)
    expect(cssFiles['../components/ui/ui.css']).toMatch(/\.ui-compact-picker-options \[role='option'\]:hover\s*\{[^}]*background:\s*var\(--color-hover\);/s)
    expect(conversationStyles).toMatch(/\.composer-suggestion-menu\s*\{[^}]*width:\s*min\(547px, 100%\);[^}]*max-height:\s*320px;[^}]*border-radius:\s*var\(--radius-xl\);[^}]*box-shadow:\s*var\(--shadow-3\);/s)
    expect(conversationStyles).toMatch(/\.composer-suggestion-viewport\s*\{[^}]*overflow-y:\s*auto;[^}]*overscroll-behavior:\s*contain;/s)
    expect(conversationStyles).toMatch(/@media \(forced-colors: active\)[\s\S]*\.composer-input-backdrop\s*\{\s*display:\s*none;/s)
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
