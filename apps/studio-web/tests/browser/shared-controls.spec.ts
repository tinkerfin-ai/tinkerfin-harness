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

for (const theme of ['light', 'dark']) {
  test(`图标切换状态始终透明且保留选中颜色与键盘焦点 ${theme}`, async ({ page, controlsOrigin }) => {
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await page.addInitScript(() => localStorage.setItem('tinkerfin:language', 'zh-CN'))
    await page.goto(`${controlsOrigin}${fixture}?example=icons&theme=${theme}`)
    const brand = await page.evaluate(() => {
      const probe = document.createElement('span')
      probe.style.color = 'var(--color-brand-text)'
      document.body.append(probe)
      try { return getComputedStyle(probe).color } finally { probe.remove() }
    })
    for (const label of ['链路', '已归档会话']) {
      const button = page.getByRole('button', { name: label, exact: true })
      if (await button.getAttribute('aria-pressed') !== 'true') await button.click()
      await expect(button).toHaveAttribute('aria-pressed', 'true')
      await page.mouse.move(0, 0)
      await expect(button).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)')
      await expect(button).toHaveCSS('box-shadow', 'none')
      await expect(button).toHaveCSS('color', brand)
      await button.hover()
      await expect(button).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)')
      await expect(button).toHaveCSS('color', brand)
      await page.mouse.move(0, 0)
      await page.keyboard.press('Tab')
      await button.focus()
      expect(await button.evaluate(element => element.matches(':focus-visible'))).toBe(true)
      await expect(button).toHaveCSS('outline-style', 'solid')
      await expect(button).toHaveCSS('outline-width', '2px')
      await expect(button).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)')
      await expect(button).toHaveCSS('box-shadow', 'none')
      await expect(button).toHaveCSS('color', brand)
      await button.hover()
      await page.mouse.down()
      try {
        await expect(button).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)')
        await expect(button).toHaveCSS('box-shadow', 'none')
        await expect(button).toHaveCSS('color', brand)
      } finally { await page.mouse.up() }
    }
    const unavailable = page.getByRole('button', { name: '不可用' })
    await expect(unavailable).toBeDisabled()
    await expect(unavailable).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)')
    await expect(unavailable).toHaveCSS('box-shadow', 'none')
    await expect(unavailable).toHaveCSS('color', brand)
    await page.emulateMedia({ forcedColors: 'active' })
    await page.mouse.move(0, 0)
    await page.keyboard.press('Tab')
    const cloud = page.getByRole('button', { name: '链路', exact: true })
    await cloud.focus()
    await expect(cloud).toHaveCSS('outline-style', 'solid')
    await expect(cloud).toHaveCSS('outline-width', '2px')
    await expect(cloud).toHaveCSS('box-shadow', 'none')
  })
}

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


async function expectAligned(page: Page) {
  const samples = await page.getByRole('region').evaluateAll(regions => {
    const icons = regions.map(region => region.querySelector('svg')!)
    const origins = icons.map(icon => icon.getAnimations()[0].startTime)
    if (origins.some(origin => origin === null)) throw new Error('动画尚未绑定时间轴')
    const sampleTime = Math.max(...origins.map(Number)) + 1200
    const transforms = icons.map((icon, index) => {
      const animation = icon.getAnimations()[0]
      const origin = Number(origins[index])
      animation.pause()
      // 在有效区间的共同采样时刻比较角度，避免负时间下的 none 造成假通过
      animation.currentTime = sampleTime - origin
      const transform = getComputedStyle(icon).transform
      animation.play()
      animation.startTime = origin
      return transform
    })
    return { origins, transforms }
  })
  expect(samples.origins).toEqual([0, 0])
  expect(samples.transforms).toHaveLength(2)
  expect(samples.transforms).not.toContain('none')
  expect(samples.transforms[0]).toBe(samples.transforms[1])
}

test('错时出现、重新挂载和动态效果恢复后的会话指示圈保持同一相位', async ({ page, controlsOrigin }) => {
  await page.emulateMedia({ reducedMotion: 'no-preference' })
  await page.goto(`${controlsOrigin}${fixture}?example=conversation-indicators`)
  await expect(page.getByRole('region', { name: '会话一' })).toBeVisible()
  await page.getByRole('button', { name: '切换第二个会话' }).click()
  await expectAligned(page)
  await page.getByRole('button', { name: '重建第二个会话' }).click()
  await expectAligned(page)
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await expect(page.getByRole('region').first().locator('svg')).toHaveCSS('animation-name', 'none')
  await expect(page.getByRole('region').last().locator('svg')).toHaveCSS('animation-name', 'none')
  await page.getByRole('region').evaluateAll(regions => {
    for (const region of regions) {
      const icon = region.querySelector('svg')!
      icon.addEventListener('animationstart', function started(event) {
        if (event.animationName !== 'workspace-spin') return
        icon.removeEventListener('animationstart', started)
        icon.setAttribute('data-animation-started', 'true')
      })
    }
  })
  await page.emulateMedia({ reducedMotion: 'no-preference' })
  await expect(page.getByRole('region').first().locator('svg')).toHaveAttribute('data-animation-started', 'true')
  await expect(page.getByRole('region').last().locator('svg')).toHaveAttribute('data-animation-started', 'true')
  await expect(page.getByRole('region').last().locator('svg')).toHaveCSS('animation-name', 'workspace-spin')
  await expectAligned(page)
})

for (const theme of ['light', 'dark']) test(`关闭共享浮层后不残留外框，后续键盘导航仍可见 ${theme}`, async ({ page, controlsOrigin }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.goto(`${controlsOrigin}${fixture}?example=focus-return&theme=${theme}`)
  for (const name of ['打开弹窗', '展开搜索', '打开选项', '选择时间', '选择日期']) {
    const trigger = page.getByRole('button', { name, exact: true })
    await trigger.click()
    await page.keyboard.press('Escape')
    await expect(trigger).toBeFocused()
    await expect(trigger).toHaveAttribute('data-ui-focus-restored', '')
    await expect(trigger).toHaveCSS('outline-width', '0px')
    await expect(trigger).toHaveCSS('box-shadow', 'none')
    await page.keyboard.press('Escape')
    await page.keyboard.press('Shift')
    await expect(trigger).toHaveCSS('outline-width', '0px')
    await expect(trigger).toHaveCSS('box-shadow', 'none')
    await page.keyboard.press('Tab')
    await expect(trigger).not.toHaveAttribute('data-ui-focus-restored')
    await page.keyboard.press('Shift+Tab')
    await expect(trigger).toBeFocused()
    expect(await trigger.evaluate(el => el.matches(':focus-visible'))).toBe(true)
  }
  const next = page.getByRole('button', { name: '后续操作', exact: true })
  await next.focus()
  await expect(next).toHaveCSS('outline-width', '2px')
})

for (const language of ['zh-CN', 'en']) for (const theme of ['light', 'dark']) {
  test(`本机资源选择、收起与焦点 ${language} ${theme}`, async ({ page, controlsOrigin }, testInfo) => {
    await page.addInitScript(language => localStorage.setItem('tinkerfin:language', language), language)
    await page.emulateMedia({ reducedMotion: 'reduce' })
    const label = language === 'en' ? 'Computer resources' : '本机资源'
    const ask = language === 'en' ? 'Ask before changes' : '修改前确认'
    const automatic = language === 'en' ? 'Run automatically' : '自动执行'
    for (const width of [320, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 })
      await page.goto(`${controlsOrigin}${fixture}?example=icons&theme=${theme}&deviceState=online`)
      const trigger = page.getByRole('button', { name: label, exact: true })
      await trigger.focus()
      await page.keyboard.press('Enter')
      const panel = page.getByRole('dialog', { name: label, exact: true })
      await expect(panel.getByRole('radio', { name: ask, exact: true })).toBeFocused()
      await expect(panel.getByRole('button')).toHaveCount(1)
      await expect(panel.getByRole('radio')).toHaveCount(2)
      await panel.getByText(automatic, { exact: true }).click()
      await expect(panel).toBeHidden()
      await expect(trigger).toBeFocused()
      await expect(trigger).toContainText(automatic)
      await trigger.click()
      await expect(panel.getByRole('radio', { name: automatic, exact: true })).toBeChecked()
      const bounds = (await panel.boundingBox())!
      expect(bounds.x).toBeGreaterThanOrEqual(0)
      expect(bounds.x + bounds.width).toBeLessThanOrEqual(width)
      await expect(panel.getByRole('status')).toHaveText(language === 'en' ? 'Connected' : '已连接')
      const centers = await panel.evaluate(element => {
        const gear = element.querySelector('.lr-icon-button svg')!.getBoundingClientRect()
        const check = element.querySelector('input:checked + .lr-check svg')!.getBoundingClientRect()
        return [gear.x + gear.width / 2, check.x + check.width / 2]
      })
      expect(centers[0]).toBe(centers[1])
      await page.screenshot({ path: testInfo.outputPath(`computer-resources-${language}-${theme}-${width}.png`) })
      await panel.getByText(automatic, { exact: true }).click()
      await expect(panel).toBeHidden()
      await expect(trigger).toBeFocused()
      await trigger.press('Enter')
      await page.keyboard.press('Escape')
      await expect(trigger).toBeFocused()
      await expect(panel).toBeHidden()
    }
  })
}

for (const state of ['online', 'offline', 'loading', 'error', 'empty']) {
  test(`本机资源状态准确且配置入口始终可用 ${state}`, async ({ page, controlsOrigin }) => {
    await page.addInitScript(() => localStorage.setItem('tinkerfin:language', 'zh-CN'))
    await page.goto(`${controlsOrigin}${fixture}?example=icons&deviceState=${state}&locked=true`)
    const trigger = page.getByRole('button', { name: '本机资源', exact: true })
    await expect(trigger).toContainText(({ online: '修改前确认', offline: '未连接', loading: '加载中', error: '加载失败', empty: '去连接' })[state]!)
    await trigger.click()
    const panel = page.getByRole('dialog', { name: '本机资源', exact: true })
    if (state === 'online' || state === 'offline') {
      await expect(panel.getByRole('status')).toHaveText(state === 'online' ? '已连接' : '未连接')
      await expect(panel.getByRole('button')).toHaveCount(1)
      await expect(panel.getByRole('radio')).toHaveCount(state === 'online' ? 2 : 0)
      for (const radio of await panel.getByRole('radio').all()) await expect(radio).toBeDisabled()
      await panel.getByRole('button', { name: '打开本地设备设置', exact: true }).click()
    }
    await expect(panel).toBeHidden()
    await expect(page.getByText('设备设置已打开', { exact: true })).toBeVisible()
  })
}
