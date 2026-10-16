import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { withBuiltPreview } from '../server/http-servers.mjs'
import { test as base, expect, type Page } from '@playwright/test'

/** 真实组件由独立本地服务承载，缓存和监听端口仅归本轮测试所有 */
const test = base.extend<Record<never, never>, { controlsOrigin: string }>({
  controlsOrigin: [async ({ browserName }, use) => {
    const directory = await mkdtemp(join(tmpdir(), `studio-controls-${browserName}-`))
    try {
      await withBuiltPreview({ root: resolve('.'), configFile: false, cacheDir: directory, logLevel: 'error',
        esbuild: { jsx: 'automatic' },
        build: { rollupOptions: { input: resolve('tests/browser/fixtures/shared-controls.html') } },
        preview: { host: '127.0.0.1', port: 0, strictPort: true },
      }, async ({ origin }) => {
        await use(origin)
      })
    } finally {
      await rm(directory, { recursive: true, force: true })
    }
  }, { scope: 'worker' }],
})
const fixture = '/tests/browser/fixtures/shared-controls.html'

for (const theme of ['light', 'dark']) for (const width of [320, 768, 1024, 1440]) {
  test(`共享视觉变体按公开参数渲染 ${theme} ${width}`, async ({ page, controlsOrigin }) => {
    await page.setViewportSize({ width, height: 900 })
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await page.goto(`${controlsOrigin}${fixture}?theme=${theme}`)
    await expect(page.getByRole('heading', { name: '共享控件验证' })).toBeVisible()
    await page.evaluate(() => document.fonts.ready)
    for (const size of ['md', 'lg']) for (const shape of ['round', 'capsule', 'standard']) {
      const surface = await page.getByRole('textbox', { name: `输入 ${size} ${shape}` }).evaluate(input => {
        // 查找实际绘制字段边框的表面，不固定包裹层级
        let element: Element | null = input
        while (element && getComputedStyle(element).borderTopStyle === 'none') element = element.parentElement
        if (!element) throw new Error('输入框缺少可见边界')
        const style = getComputedStyle(element)
        return { height: element.getBoundingClientRect().height, radius: style.borderTopLeftRadius }
      })
      expect(surface).toEqual({ height: size === 'md' ? 40 : 44, radius: shape === 'standard' ? '8px' : '22px' })
    }
    for (const [density, height] of [['regular', 44], ['medium', 40], ['compact', 32]] as const) {
      await expect(page.getByRole('tablist', { name: `页签 ${density}` })).toHaveCSS('height', `${height}px`)
    }
    for (const [size, height] of [['sm', 22], ['md', 30], ['lg', width <= 440 ? 38 : 42]] as const) {
      await expect(page.locator(`.fixture-brand-${size}`)).toHaveCSS('height', `${height}px`)
    }
    await expect(page.locator('.fixture-mark img')).toHaveCSS('height', '28px')
    for (const [size, pixels] of [['sm', 32], ['lg', 64]] as const) {
      await expect(page.locator(`.fixture-avatar-${size}`)).toHaveCSS('width', `${pixels}px`)
      await expect(page.locator(`.fixture-avatar-${size}`)).toHaveCSS('height', `${pixels}px`)
    }
    for (const [variant, backgroundToken, foregroundToken] of [
      ['primary', '--color-brand', '--color-on-brand'],
      ['secondary', '--color-layer-1', '--color-text-primary'],
      ['solid', '--color-text-primary', '--color-layer-1'],
    ]) {
      const button = page.getByRole('button', { name: `操作 ${variant}` })
      await expect(button).toHaveCSS('border-radius', '12px')
      await expect(button).toHaveCSS('height', '36px')
      const expected = await page.evaluate(([background, foreground]) => {
        const probe = document.createElement('span')
        probe.style.backgroundColor = `var(${background})`
        probe.style.color = `var(${foreground})`
        document.body.append(probe)
        try { const style = getComputedStyle(probe); return { background: style.backgroundColor, foreground: style.color } }
        finally { probe.remove() }
      }, [backgroundToken, foregroundToken])
      await expect(button).toHaveCSS('background-color', expected.background)
      await expect(button).toHaveCSS('color', expected.foreground)
    }

  })
}

const openScrollbar = async (page: Page, origin: string, query = '') => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.clock.install({ time: new Date('2030-01-01T00:00:00Z') })
  await page.goto(`${origin}${fixture}?example=scrollbar&${query}`)
  await expect(page.getByRole('region', { name: '可滚动内容' })).toBeVisible()
  await page.clock.pauseAt(new Date('2030-01-01T00:01:00Z'))
  // 滚动条按设计不进入辅助技术；只用选择器定位绘制目标，结果读取真实样式
  return { viewport: page.getByRole('region', { name: '可滚动内容' }),
    overlay: page.locator('.ui-overlay-scrollbar'), thumb: page.locator('.ui-overlay-scrollbar__thumb') }
}

test('滚动条悬停、键盘与触控按独立意图显隐，卸载释放区域', async ({ page, controlsOrigin }) => {
  const { viewport, overlay } = await openScrollbar(page, controlsOrigin)
  await expect(overlay).toHaveCSS('opacity', '0')
  await viewport.hover()
  await page.clock.runFor(2000)
  await expect(overlay).toHaveCSS('opacity', '1')
  await page.mouse.move(900, 800)
  await page.clock.runFor(999)
  await expect(overlay).toHaveCSS('opacity', '1')
  await page.clock.runFor(1)
  await expect(overlay).toHaveCSS('opacity', '0')
  const button = page.getByRole('button', { name: '查看详情' })
  await button.click()
  await page.mouse.move(900, 800)
  await page.clock.runFor(1000)
  await expect(button).toBeFocused()
  await expect(overlay).toHaveCSS('opacity', '0')
  await button.press('a')
  await page.clock.runFor(1000)
  await expect(overlay).toHaveCSS('opacity', '1')
  await button.click()
  await page.mouse.move(900, 800)
  await page.clock.runFor(1000)
  await expect(button).toBeFocused()
  await expect(overlay).toHaveCSS('opacity', '0')
  await button.press('a')
  await expect(overlay).toHaveCSS('opacity', '1')
  await page.getByRole('button', { name: '后续操作' }).click()
  await page.clock.runFor(1000)
  await expect(overlay).toHaveCSS('opacity', '0')
  await viewport.dispatchEvent('pointerenter', { pointerType: 'touch' })
  await expect(overlay).toHaveCSS('opacity', '0')
  await viewport.evaluate(element => { element.scrollTop += 20; element.dispatchEvent(new Event('scroll')) })
  await page.clock.runFor(32)
  await expect(overlay).toHaveCSS('opacity', '1')
  await page.clock.runFor(1000)
  await expect(overlay).toHaveCSS('opacity', '0')
  await page.getByRole('button', { name: '移除滚动区域' }).click()
  await page.clock.runFor(2000)
  await expect(viewport).toHaveCount(0)
})

for (const ending of ['pointerup', 'pointercancel', 'lostpointercapture']) {
  test(`滚动条拖拽在 ${ending} 后恢复自动隐藏`, async ({ page, controlsOrigin }) => {
    const { viewport, overlay, thumb } = await openScrollbar(page, controlsOrigin)
    await viewport.hover()
    await expect(overlay).toHaveCSS('opacity', '1')
    await thumb.evaluate(element => element.addEventListener('pointerdown', event => {
      if (!(event instanceof PointerEvent)) throw new Error('拖拽需要指针事件')
      element.setAttribute('data-test-pointer', String(event.pointerId))
    }, { once: true }))
    await thumb.hover()
    await page.mouse.down()
    const pointerId = Number(await thumb.getAttribute('data-test-pointer'))
    try {
      await page.mouse.move(900, 800)
      await page.clock.runFor(1000)
      await expect(overlay).toHaveCSS('opacity', '1')
      if (ending === 'pointerup') await page.mouse.up()
      else if (ending === 'pointercancel') await thumb.dispatchEvent(ending, { pointerId, pointerType: 'mouse' })
      else {
        await thumb.evaluate((element, id) => element.releasePointerCapture(id), pointerId)
        await page.mouse.move(901, 800)
      }
      await page.mouse.move(902, 800)
      await page.clock.runFor(1000)
      await expect(overlay).toHaveCSS('opacity', '0')
    } finally {
      await page.mouse.up()
    }

  })
}

for (const axis of ['vertical', 'horizontal']) test(`常驻 ${axis} 滚动条保持可见且无溢出时隐藏`, async ({ page, controlsOrigin }) => {
  const { viewport, overlay, thumb } = await openScrollbar(page, controlsOrigin, `axis=${axis}&size=compact&visibility=persistent`)
  await expect(overlay).toHaveCSS('opacity', '1')
  const bounds = await thumb.boundingBox()
  expect(bounds).not.toBeNull()
  expect(axis === 'vertical' ? bounds!.height : bounds!.width).toBeGreaterThan(0)
  await page.mouse.move(900, 800)
  await page.clock.runFor(2000)
  await expect(overlay).toHaveCSS('opacity', '1')
  await page.getByRole('button', { name: '切换内容高度' }).click()
  await viewport.evaluate(element => new Promise<void>(resolve => {
    const observer = new ResizeObserver(() => {
      if (element.scrollWidth > element.clientWidth || element.scrollHeight > element.clientHeight) return
      observer.disconnect()
      resolve()
    })
    observer.observe(element.firstElementChild!)
  }))
  await page.clock.runFor(32)
  await expect(overlay).toHaveCSS('opacity', '0')
})

for (const theme of ['light', 'dark']) for (const width of [320, 768, 1024, 1440]) {
  test(`加载反馈在页面和功能样式中保持一致 ${theme} ${width}`, async ({ page, controlsOrigin }, info) => {
    await page.setViewportSize({ width, height: 900 })
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await page.goto(`${controlsOrigin}${fixture}?example=loading&theme=${theme}`)
    await page.evaluate(() => document.fonts.ready)
    let reference: unknown
    for (const name of ['页面', '紧凑区域', '链路', '链路详情', '搜索', '工作区']) {
      const region = page.getByRole('region', { name, exact: true })
      const status = region.getByRole('status')
      await expect(status).toHaveAttribute('aria-busy', 'true')
      const visual = await status.evaluate(element => {
        const style = getComputedStyle(element)
        const icon = element.querySelector('svg')!
        const text = [...element.children].find(child => child.textContent === '正在加载历史会话')!
        const titleStyle = getComputedStyle(text)
        return { border: style.borderTopWidth, background: style.backgroundColor, shadow: style.boxShadow,
          gap: style.columnGap, fontSize: titleStyle.fontSize, weight: titleStyle.fontWeight,
          iconWidth: icon.getBoundingClientRect().width, iconColor: getComputedStyle(icon).color,
          animation: getComputedStyle(icon).animationName,
          withinViewport: element.getBoundingClientRect().right <= innerWidth,
          centered: Math.abs(element.getBoundingClientRect().left + element.getBoundingClientRect().width / 2
            - (element.parentElement!.getBoundingClientRect().left + element.parentElement!.getBoundingClientRect().width / 2)) < 1 }
      })
      expect(visual).toMatchObject({ border: '0px', background: 'rgba(0, 0, 0, 0)', shadow: 'none', gap: '8px', fontSize: '13px', iconWidth: 18, animation: 'none', withinViewport: true, centered: true })
      if (reference) expect(visual).toEqual(reference)
      else reference = visual
    }
    const long = page.getByRole('region', { name: '长加载文案' }).getByRole('status')
    expect(await long.evaluate(element => element.scrollWidth <= element.clientWidth && element.getBoundingClientRect().right <= innerWidth)).toBe(true)
    await expect(page.getByRole('region', { name: '错误恢复' }).getByRole('button', { name: '重新加载' })).toBeVisible()
    await page.screenshot({ path: info.outputPath(`loading-${theme}-${width}.png`), fullPage: true, animations: 'disabled' })
  })
}

test('加载反馈的转圈遵守减少动态效果偏好', async ({ page, controlsOrigin }) => {
  await page.emulateMedia({ reducedMotion: 'no-preference' })
  await page.goto(`${controlsOrigin}${fixture}?example=loading`)
  const icon = page.getByRole('region', { name: '页面', exact: true }).getByRole('status').locator('svg')
  await expect(icon).toHaveCSS('animation-name', 'ui-spin')
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await expect(icon).toHaveCSS('animation-name', 'none')
})
