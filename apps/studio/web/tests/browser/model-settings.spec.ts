import { expect, test, type Page } from '@playwright/test'

const user = { user_id: 17, username: 'settings-test', display_name: '配置验收', avatar_url: null, roles: [], disabled: false }
const models = [
  {model_id:'pro',display_name:'deepseek-v4-pro',purpose:'chat',provider:'deepseek',model_name:'deepseek-v4-pro',base_url:'https://api.deepseek.com',has_key:true,image_support:'unknown',reasoning_enabled:true,enabled:true,is_default:true,sort_order:0,generation_options:{}},
  {model_id:'vision',display_name:'DeepSeek-V4-Flash-Vision-Exp',purpose:'chat',provider:'deepseek',model_name:'deepseek-v4-flash-vision-exp',base_url:'https://api.deepseek.com',has_key:true,image_support:'supported',reasoning_enabled:true,enabled:true,is_default:false,sort_order:1,generation_options:{}},
  {model_id:'image',display_name:'Seedream 5.0 Lite',purpose:'image',provider:'openai',model_name:'doubao-seedream-5-0-lite-260128',base_url:'https://ark.cn-beijing.volces.com/api/v3',has_key:true,image_support:'unknown',reasoning_enabled:false,enabled:true,is_default:true,sort_order:2,generation_options:{size:'2K',output_format:'png',watermark:false,response_format:'url',sequential_image_generation:'disabled'}},
]
async function setup(page: Page) {
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    let data: unknown = {}
    if (path === '/api/auth/me') data = {expires_at:'2099-01-01T00:00:00.000Z',user}
    else if (path === '/api/models') data = {items:models.filter(model=>model.purpose==='chat').map(model=>({modelId:model.model_id,displayName:model.display_name,reasoningEnabled:model.reasoning_enabled,imageSupport:model.image_support,isDefault:model.is_default})),defaultModelId:'pro'}
    else if (path === '/api/models/configurations') data = models
    else if (path === '/api/models/configurations/test') {
      const body = route.request().postDataJSON()
      data = {kind:body.kind,outcome:'success',elapsed_ms:125,code:body.kind==='basic'?'model_listed':'text_received',text:body.kind==='basic'?null:'OK',image:null}
    }
    else if (path.endsWith('/default')) data = null
    else if (path === '/api/conversation/config') data = {dayRanges:[7,30]}
    else if (path === '/api/conversation/history') data = {items:[],nextCursor:null}
    await route.fulfill({json:{code:0,message:'success',data}})
  })
}
async function openSettings(page: Page, language: 'zh-CN'|'en', theme: 'light'|'dark') {
  await page.addInitScript(({user,language,theme}) => {
    localStorage.setItem('tinkerfin.auth.session',JSON.stringify({token:'isolated-browser-test',tokenType:'Bearer',expiresAt:'2099-01-01T00:00:00.000Z',user}))
    localStorage.setItem('tinkerfin:language',language)
    localStorage.setItem('tinkerfin:theme',theme)
  }, {user,language,theme})
  await page.goto('/')
  if ((page.viewportSize()?.width ?? 1440) < 768) {
    await page.getByRole('button', {name: language === 'en' ? 'Open navigation' : '打开导航', exact: true}).click()
  }
  await page.getByRole('button',{name:language==='en'?'Open user menu':'打开用户菜单'}).click()
  await page.getByRole('menuitem',{name:language==='en'?'Settings':'设置',exact:true}).click()
  await page.getByRole('button',{name:language==='en'?'Models':'模型配置',exact:true}).click()
}

for (const language of ['zh-CN','en'] as const) for (const theme of ['light','dark'] as const) {
  test(`模型配置布局 ${language} ${theme}`, async ({page}) => {
    test.setTimeout(90_000)
    const failures: string[] = []
    page.on('pageerror', error => failures.push(error.message))
    await setup(page)
    await openSettings(page,language,theme)
    const edit = language==='en'?'Edit':'编辑'
    const save = language==='en'?'Save':'保存'
    const back = language==='en'?'Back to models':'返回模型列表'
    const dialog = page.getByRole('dialog',{name:language==='en'?'Settings':'设置',exact:true})
    for (const width of [320,768,1024,1440]) {
      await page.setViewportSize({width,height:960})
      await expect(page.getByRole('button',{name:edit,exact:true})).toHaveCount(3)

      await page.getByRole('button',{name:edit,exact:true}).last().click()
      await expect(dialog.getByRole('heading', {name: language === 'en' ? 'Models' : '模型配置', exact: true})).toHaveCount(0)
      await expect(page.getByRole('button', {name: back, exact: true}).locator('svg')).toHaveCount(1)
      const returnAlignment = await page.evaluate(() => {
        const button = document.querySelector('.settings-models__back')!
        const icon = button.querySelector('svg')!.getBoundingClientRect()
        const label = button.querySelector('.ui-button__label')!.getBoundingClientRect()
        const form = document.querySelector('.settings-models__form')!.getBoundingClientRect()
        const box = button.getBoundingClientRect()
        return {
          centers: Math.abs(icon.top + icon.height / 2 - label.top - label.height / 2),
          left: Math.abs(box.left - form.left),
          gap: label.left - icon.right,
          expectedGap: parseFloat(getComputedStyle(button).columnGap),
        }
      })
      expect(returnAlignment.centers).toBeLessThanOrEqual(1)
      expect(returnAlignment.left).toBeLessThanOrEqual(1)
      expect(Math.abs(returnAlignment.gap - returnAlignment.expectedGap)).toBeLessThanOrEqual(1)

      await page.getByText(language==='en'?'Advanced parameters':'高级参数',{exact:true}).click()
      await page.getByRole('textbox',{name:language==='en'?'Advanced parameters JSON':'高级参数 JSON'}).waitFor()

      const dimensions = await page.evaluate(() => {
        const dialog = document.querySelector('.settings-dialog')!
        const save = dialog.querySelector('button[type=submit]')!
        const close = dialog.querySelector('.modal-dialog-close')!
        const control = dialog.querySelector('.settings-models .ui-text-field__control')!
        const choice = dialog.querySelector('.settings-models__purpose .is-selected')!
        const alignedRows: number[] = []
        for (const grid of dialog.querySelectorAll('.settings-models__grid')) {
          const rows = new Map<number, number[]>()
          for (const field of grid.querySelectorAll('.ui-text-field, .settings-models__choice')) {
            const label = field.querySelector('.ui-text-field__label, :scope > span')!
            const input = field.querySelector('.ui-text-field__control, .settings-choice-trigger')!
            const y = Math.round(label.getBoundingClientRect().top)
            const row = rows.get(y) ?? []
            row.push(input.getBoundingClientRect().top)
            rows.set(y, row)
          }
          for (const row of rows.values()) if (row.length > 1) alignedRows.push(Math.max(...row) - Math.min(...row))
        }
        return {
          alignedRows,
          footerBorder: getComputedStyle(dialog.querySelector('.settings-models__actions')!).borderTopWidth,
          editorBorder: getComputedStyle(dialog.querySelector('.model-options-editor')!).borderWidth,
          footerTop: dialog.querySelector('.settings-models__actions')!.getBoundingClientRect().top,
          viewportBottom: dialog.querySelector('.settings-models__scroll')!.getBoundingClientRect().bottom,
          trackBottom: dialog.querySelector('.ui-overlay-scrollbar')!.getBoundingClientRect().bottom,
          overflows: dialog.scrollWidth > dialog.clientWidth + 1,
          saveHeight: save.getBoundingClientRect().height,
          alignment: Math.abs(save.getBoundingClientRect().right-close.getBoundingClientRect().right),
          radius: getComputedStyle(control).borderRadius,
          buttonRadius: getComputedStyle(save).borderRadius,
          color: getComputedStyle(choice).backgroundColor,
          expectedColor: getComputedStyle(document.documentElement).getPropertyValue('--color-brand-soft').trim(),
        }
      })
      if (dimensions.overflows) console.log('layout overflow', language, theme, width, await page.evaluate(() => Array.from(document.querySelectorAll('.settings-dialog *')).filter(element => element.getBoundingClientRect().right > document.querySelector('.settings-dialog')!.getBoundingClientRect().right + 1).slice(0, 12).map(element => ({tag:element.tagName,classes:element.className,width:element.getBoundingClientRect().width}))))
      expect(dimensions.overflows).toBe(false)
      if (width >= 768) expect(dimensions.alignedRows.length).toBeGreaterThanOrEqual(2)
      for (const offset of dimensions.alignedRows) expect(offset).toBeLessThanOrEqual(1)
      expect(dimensions.footerBorder).toBe('0px')
      expect(dimensions.editorBorder).toBe('0px')
      expect(Math.abs(dimensions.footerTop - dimensions.viewportBottom)).toBeLessThanOrEqual(1)
      expect(dimensions.trackBottom).toBeLessThanOrEqual(dimensions.footerTop)
      expect(dimensions.saveHeight).toBe(32)
      expect(dimensions.alignment).toBeLessThanOrEqual(1)
      expect(dimensions.radius).toBe(dimensions.buttonRadius)
      await expect(page.getByRole('button',{name:save,exact:true}).locator('svg')).toHaveCount(0)
      await page.getByRole('button',{name:back,exact:true}).click()
    }
    expect(failures).toEqual([])
  })
}

test('草稿 JSON 与测试结果可恢复且不保存配置', async ({page}) => {
  await setup(page)
  await openSettings(page,'zh-CN','light')
  let saved = false
  page.on('request', request => { if (request.method()==='PUT') saved=true })
  await page.getByRole('button',{name:'编辑',exact:true}).last().click()
  await page.getByText('高级参数',{exact:true}).click()
  const editor = page.getByRole('textbox',{name:'高级参数 JSON'})
  await editor.fill('{"watermark":false,}')
  await expect(page.getByRole('button',{name:'保存',exact:true})).toBeDisabled()
  await expect(page.getByRole('alert')).toContainText('行')

  await editor.fill('{"watermark":false}')
  await expect(page.getByRole('button',{name:'保存',exact:true})).toBeEnabled()
  await page.getByText('测试当前配置',{exact:true}).click()
  await page.getByRole('button',{name:'检查连接',exact:true}).click()
  await expect(page.getByText('基础检查通过，服务已列出该模型',{exact:true})).toBeVisible()
  await page.getByLabel('Model ID',{exact:true}).fill('changed-model')
  await expect(page.getByText('配置已修改，此结果已过期，请重新测试',{exact:true})).toBeVisible()
  expect(saved).toBe(false)
})

test('编辑器下载失败后仍可编辑并校验当前草稿', async ({page}) => {
  await setup(page)
  await page.route('**/ModelOptionsEditor-*.js', route => route.abort(), {times: 1})
  await openSettings(page, 'zh-CN', 'light')
  await page.getByRole('button', {name: '编辑', exact: true}).last().click()
  await page.getByLabel('显示名称', {exact: true}).fill('尚未保存的生图配置')
  await page.getByText('高级参数', {exact: true}).click()
  await expect(page.getByText('高级编辑器不可用，可继续使用纯文本编辑', {exact: true})).toBeVisible()
  await expect(page.getByRole('textbox', {name: '高级参数 JSON'})).toBeVisible()
  await expect(page.getByLabel('显示名称', {exact: true})).toHaveValue('尚未保存的生图配置')
  const editor = page.getByRole('textbox', {name: '高级参数 JSON'})
  await expect(editor).toHaveValue(/watermark/)
  await editor.fill('{"watermark":false,}')
  await expect(page.getByRole('button', {name: '保存', exact: true})).toBeDisabled()
  await editor.fill('{"watermark":false}')
  await expect(page.getByRole('button', {name: '保存', exact: true})).toBeEnabled()

})

test('模型设置触控操作和减少动态效果保持可用', async ({browser}) => {
  for (const theme of ['light', 'dark'] as const) {
    const context = await browser.newContext({viewport: {width: 320, height: 960}, hasTouch: true, reducedMotion: 'reduce'})
    try {
      const page = await context.newPage()
      await setup(page)
      await openSettings(page, 'zh-CN', theme)
      for (const button of await page.locator('.settings-models button').all()) {
        const box = await button.boundingBox()
        expect(box?.width).toBeGreaterThanOrEqual(44)
        expect(box?.height).toBeGreaterThanOrEqual(44)
      }
      await page.getByRole('button', {name: '编辑', exact: true}).last().click()
      for (const label of ['显示名称', 'Model ID', 'Base URL', 'API Key']) {
        const input = page.getByLabel(label, {exact: true})
        expect((await input.boundingBox())!.height).toBeGreaterThanOrEqual(44)
      }
      await page.getByText('高级参数', {exact: true}).click()
      const editor = page.getByRole('textbox', {name: '高级参数 JSON'})
      await editor.focus()
      await page.keyboard.press('Tab')
      await expect(editor).not.toBeFocused()
      const save = page.getByRole('button', {name: '保存', exact: true})
      await expect(save).toHaveCSS('transition-duration', '0s')
      const box = await save.boundingBox()
      expect(box?.height).toBeGreaterThanOrEqual(44)
      expect(box?.width).toBeGreaterThanOrEqual(44)

    } finally {
      await context.close()
    }
  }
})

test('展开测试配置后完整操作区进入可见范围', async ({page}) => {
  await setup(page)
  await openSettings(page, 'zh-CN', 'light')
  await page.setViewportSize({width: 1024, height: 700})
  await page.getByRole('button', {name: '编辑', exact: true}).last().click()
  const summary = page.getByText('测试当前配置', {exact: true})
  await summary.click()
  const geometry = () => page.evaluate(() => {
    const viewport = document.querySelector('.settings-models__scroll')!.getBoundingClientRect()
    const footer = document.querySelector('.settings-models__actions')!.getBoundingClientRect()
    const panel = document.querySelector('.settings-models__test-disclosure')!.getBoundingClientRect()
    return {top: panel.top - viewport.top, bottom: Math.min(viewport.bottom, footer.top) - panel.bottom}
  })
  await expect.poll(async () => (await geometry()).top).toBeGreaterThanOrEqual(0)
  await expect.poll(async () => (await geometry()).bottom).toBeGreaterThanOrEqual(0)
  await page.getByRole('button', {name: '检查连接', exact: true}).click()
  await expect(page.getByText('基础检查通过，服务已列出该模型', {exact: true})).toBeVisible()
  await expect.poll(async () => (await geometry()).bottom).toBeGreaterThanOrEqual(0)

})

test('延迟编辑器展开后可见且滚动条匹配实际内容', async ({page}) => {
  let releaseEditor!: () => void
  const ready = new Promise<void>(resolve => { releaseEditor = resolve })
  await setup(page)
  await page.route('**/ModelOptionsEditor-*.js', async route => { await ready; await route.continue() })
  try {
    await openSettings(page, 'zh-CN', 'dark')
    await page.setViewportSize({width: 1024, height: 700})
    await page.getByRole('button', {name: '编辑', exact: true}).last().click()
    await page.getByText('高级参数', {exact: true}).click()
    await expect(page.getByText('正在加载编辑器…', {exact: true})).toBeVisible()
    releaseEditor()
    await page.getByRole('textbox', {name: '高级参数 JSON'}).waitFor()
    await expect.poll(() => page.evaluate(() => {
      const viewport = document.querySelector('.settings-models__scroll')!.getBoundingClientRect()
      const summary = document.querySelector('.settings-models__advanced > summary')!.getBoundingClientRect()
      return summary.top - viewport.top
    })).toBeGreaterThanOrEqual(0)
    await expect.poll(() => page.evaluate(() => {
      const viewport = document.querySelector('.settings-models__scroll')!
      const track = document.querySelector('.settings-models .ui-overlay-scrollbar')!.getBoundingClientRect()
      const thumb = document.querySelector('.settings-models .ui-overlay-scrollbar__thumb')!.getBoundingClientRect()
      return Math.abs(thumb.height - Math.max(24, track.height * viewport.clientHeight / viewport.scrollHeight))
    })).toBeLessThan(1)

  } finally {
    releaseEditor()
  }
})

test('用户主动滚动或收起后延迟编辑器不抢回阅读位置', async ({page}) => {
  let releaseEditor!: () => void
  const ready = new Promise<void>(resolve => { releaseEditor = resolve })
  await setup(page)
  await page.route('**/ModelOptionsEditor-*.js', async route => { await ready; await route.continue() })
  try {
    await openSettings(page, 'zh-CN', 'light')
    await page.getByRole('button', {name: '编辑', exact: true}).last().click()
    await page.getByText('高级参数', {exact: true}).click()
    await expect(page.getByText('正在加载编辑器…', {exact: true})).toBeVisible()
    const viewport = page.getByRole('region', {name: '设置', exact: true})
    const box = (await viewport.boundingBox())!
    await page.mouse.move(box.x + box.width / 2, box.y + 30)
    await page.mouse.wheel(0, -3000)
    await expect.poll(() => viewport.evaluate(element => element.scrollTop)).toBe(0)
    releaseEditor()
    await page.getByRole('textbox', {name: '高级参数 JSON'}).waitFor({state: 'attached'})
    await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))))
    expect(await viewport.evaluate(element => element.scrollTop)).toBe(0)
    await page.getByText('高级参数', {exact: true}).click()
    await expect(page.getByRole('textbox', {name: '高级参数 JSON'})).toHaveCount(0)
    await page.getByText('高级参数', {exact: true}).click()
    await expect(page.getByRole('textbox', {name: '高级参数 JSON'})).toBeVisible()
  } finally {
    releaseEditor()
  }
})

test('列表测试入口直接露出测试操作，折叠后不把页面拉回', async ({page}) => {
  await setup(page)
  await openSettings(page, 'zh-CN', 'light')
  await page.getByRole('button', {name: '测试', exact: true}).last().click()
  await expect.poll(() => page.evaluate(() => {
    const panel = document.querySelector('.settings-models__test-disclosure')!.getBoundingClientRect()
    const footer = document.querySelector('.settings-models__actions')!.getBoundingClientRect()
    return footer.top - panel.bottom
  })).toBeGreaterThanOrEqual(0)
  await page.getByText('测试当前配置', {exact: true}).click()
  await expect(page.getByRole('button', {name: '生成测试图片', exact: true})).toBeHidden()
  await page.getByRole('button', {name: '返回模型列表', exact: true}).click()
  await expect(page.getByRole('button', {name: '添加模型', exact: true})).toBeVisible()
})

test('固定操作栏不随表单滚动并使用统一必填提示和保存', async ({page}) => {
  await page.emulateMedia({reducedMotion: 'reduce'})
  await setup(page)
  const saved: unknown[] = []
  await page.route('**/api/models/configurations/*', async route => {
    if (route.request().method() !== 'PUT') { await route.fallback(); return }
    saved.push(route.request().postDataJSON())
    await route.fulfill({json: {code: 0, message: 'success', data: null}})
  })
  await openSettings(page, 'zh-CN', 'light')
  await page.setViewportSize({width: 768, height: 560})
  await page.getByRole('button', {name: '添加模型', exact: true}).click()
  for (const input of await page.locator('.settings-models input[required]').all()) {
    expect((await input.getAttribute('placeholder'))?.trim()).toBeTruthy()
  }
  const save = page.getByRole('button', {name: '保存', exact: true})
  await save.click()
  await expect(page.getByLabel('显示名称', {exact: true})).toBeFocused()
  await expect(page.locator('.settings-models form')).toHaveAttribute('novalidate')
  await expect(page.getByText('请输入显示名称', {exact: true})).toBeVisible()
  expect(saved).toHaveLength(0)
  const before = (await save.boundingBox())!
  const viewport = page.getByRole('region', {name: '设置', exact: true})
  await viewport.evaluate(element => { element.scrollTop = element.scrollHeight })
  const after = (await save.boundingBox())!
  expect(after).toEqual(before)
  expect(await viewport.evaluate(element => element.querySelector('button[type="submit"]') !== null)).toBe(false)
  await page.getByLabel('显示名称', {exact: true}).fill('新模型')
  await page.getByLabel('Model ID', {exact: true}).fill('example-model')
  await page.getByLabel('API Key', {exact: true}).fill('isolated-placeholder-key')
  await save.click()
  await expect(page.getByRole('button', {name: '添加模型', exact: true})).toBeVisible()
  expect(saved).toHaveLength(1)
  expect(saved[0]).toMatchObject({display_name: '新模型', model_name: 'example-model'})
})

test('底部保存和测试只在弹窗内定位错误，不触发浏览器气泡', async ({page}) => {
  await page.emulateMedia({reducedMotion: 'reduce'})
  await page.addInitScript(() => {
    document.addEventListener('invalid', () => { document.documentElement.dataset.nativeInvalid = 'true' }, true)
  })
  await setup(page)
  await openSettings(page, 'zh-CN', 'light')
  await page.getByRole('button', {name: '编辑', exact: true}).first().click()
  const dialog = page.getByRole('dialog', {name: '设置', exact: true})
  const name = page.getByLabel('显示名称', {exact: true})
  await name.fill('')
  const requests: string[] = []
  page.on('request', request => { if (['PUT', 'POST'].includes(request.method())) requests.push(request.url()) })
  for (const action of ['保存', '检查连接']) {
    await page.getByText('测试当前配置', {exact: true}).click()
    const before = await dialog.boundingBox()
    const pageOffset = await page.evaluate(() => ({x: scrollX, y: scrollY}))
    await page.getByRole('button', {name: action, exact: true}).click()
    await expect(name).toBeFocused()
    await expect(name).toHaveAttribute('aria-invalid', 'true')
    await expect(page.getByText('请输入显示名称', {exact: true})).toBeVisible()
    expect(await dialog.boundingBox()).toEqual(before)
    expect(await page.evaluate(() => ({x: scrollX, y: scrollY}))).toEqual(pageOffset)
    expect(await page.locator('html').getAttribute('data-native-invalid')).toBeNull()
    expect(requests).toEqual([])
    await name.fill('已修正')
    await expect(name).toHaveAttribute('aria-invalid', 'false')
    await name.fill('')
    if (action === '保存') await page.getByText('测试当前配置', {exact: true}).click()
  }

})

test('基础检查未确认时明确提示下一步实际测试', async ({page}) => {
  await setup(page)
  await page.route('**/api/models/configurations/test', route => route.fulfill({json: {
    code: 0, message: 'success', data: {kind: 'basic', outcome: 'inconclusive', elapsed_ms: 700, code: 'model_not_listed', text: null, image: null},
  }}))
  await openSettings(page, 'zh-CN', 'light')
  await page.getByRole('button', {name: '测试', exact: true}).last().click()
  await page.getByRole('button', {name: '检查连接', exact: true}).click()
  await expect(page.getByText('还不能确认这个模型能否使用', {exact: true})).toBeVisible()
  await expect(page.getByText('请点击“生成测试图片”，看看能否成功生成', {exact: true})).toBeVisible()

})

test('对话能力选择与推理开关水平居中且没有多余密钥提示', async ({page}) => {
  await setup(page)
  await openSettings(page, 'zh-CN', 'light')
  await page.getByRole('button', {name: '编辑', exact: true}).first().click()
  const choice = page.getByRole('button', {name: '图片输入能力', exact: true})
  const reasoning = page.getByRole('checkbox', {name: '启用推理', exact: true})
  await choice.scrollIntoViewIfNeeded()
  const choiceBox = (await choice.boundingBox())!
  const reasoningBox = (await reasoning.boundingBox())!
  expect(Math.abs(choiceBox.y + choiceBox.height / 2 - reasoningBox.y - reasoningBox.height / 2)).toBeLessThanOrEqual(1)
  await expect(page.getByText('密钥不会在列表中显示', {exact: true})).toHaveCount(0)

})

test('保存失败后返回或取消不会把编辑错误和密钥带入列表', async ({page}) => {
  await setup(page)
  await page.route('**/api/models/configurations/*', async route => {
    if (route.request().method() !== 'PUT') { await route.fallback(); return }
    await route.fulfill({status: 409, json: {code: 409001, message: '该模型仍有运行或审批未结束，请结束后再修改或删除', data: null}})
  })
  await openSettings(page, 'zh-CN', 'light')
  for (const leave of ['返回模型列表', '取消']) {
    await page.getByRole('button', {name: '编辑', exact: true}).first().click()
    await expect(page.getByLabel('API Key', {exact: true})).toHaveValue('')
    await page.getByLabel('API Key', {exact: true}).fill('isolated-unsaved-key')
    await page.getByRole('button', {name: '保存', exact: true}).click()
    await expect(page.getByRole('alert')).toContainText('该模型仍有运行或审批未结束')
    await page.getByRole('button', {name: leave, exact: true}).click()
    await expect(page.getByRole('button', {name: '添加模型', exact: true})).toBeVisible()
    await expect(page.getByRole('dialog', {name: '设置', exact: true}).getByRole('alert')).toHaveCount(0)
    await expect(page.getByLabel('API Key', {exact: true})).toHaveCount(0)
    await page.getByRole('button', {name: '关闭提示：该模型仍有运行或审批未结束，请结束后再修改或删除', exact: true}).click()
    await expect(page.getByRole('alert')).toHaveCount(0)
  }
})
