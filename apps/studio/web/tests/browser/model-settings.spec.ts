import { expect, test, type Page } from '@playwright/test'

const user = { user_id: 17, username: 'settings-test', display_name: '配置验收', avatar_url: null, roles: [], disabled: false }
const chat_options = { max_tokens:null, temperature:null, top_p:null, stop:null, reasoning_effort:null, context_window:null, keep_alive:null }
const connections = [
  { connection_id:'deepseek', display_name:'DeepSeek', provider_id:'deepseek', api_type:'openai_chat_completions', base_url:'https://api.deepseek.com', auth_type:'api_key', has_key:true },
  { connection_id:'qwen', display_name:'通义千问', provider_id:'dashscope', api_type:'openai_chat_completions', base_url:'https://dashscope.aliyuncs.com/compatible-mode/v1', auth_type:'api_key', has_key:true },
  { connection_id:'ollama', display_name:'本地 Ollama', provider_id:'ollama', api_type:'ollama', base_url:'http://localhost:11434', auth_type:'none', has_key:false },
]
const models = [
  {model_id:'pro',connection_id:'deepseek',display_name:'DeepSeek V4 Pro',purpose:'chat',model_name:'deepseek-v4-pro',image_support:'unknown',reasoning_enabled:true,enabled:true,is_default:true,sort_order:0,generation_options:{},chat_options},
  {model_id:'flash',connection_id:'deepseek',display_name:'DeepSeek V4 Flash',purpose:'chat',model_name:'deepseek-v4-flash',image_support:'unknown',reasoning_enabled:true,enabled:true,is_default:false,sort_order:1,generation_options:{},chat_options},
  {model_id:'qwen',connection_id:'qwen',display_name:'Qwen Plus',purpose:'chat',model_name:'qwen-plus',image_support:'unknown',reasoning_enabled:false,enabled:true,is_default:false,sort_order:0,generation_options:{},chat_options},
  {model_id:'local',connection_id:'ollama',display_name:'Qwen3 14B',purpose:'chat',model_name:'qwen3:14b',image_support:'unknown',reasoning_enabled:true,enabled:true,is_default:false,sort_order:0,generation_options:{},chat_options},
]
async function setup(page: Page, crowded = false) {
  const savedModels = crowded ? [...models, ...Array.from({length: 20}, (_, index) => ({...models[1], model_id: `extra-${index}`, display_name: `Model ${index}`, model_name: `model-${index}`}))] : models
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    let data: unknown = {}
    if (path === '/api/auth/me') data = {expires_at:'2099-01-01T00:00:00.000Z',user}
    else if (path === '/api/models') data = {items:models.map(model=>({modelId:model.model_id,displayName:model.display_name,connectionId:model.connection_id,connectionDisplayName:connections.find(connection=>connection.connection_id===model.connection_id)!.display_name,reasoningEnabled:model.reasoning_enabled,imageSupport:model.image_support,isDefault:model.is_default})),defaultModelId:'pro'}
    else if (path === '/api/models/settings') data = { models: savedModels, connections, providers:[{provider_id:'custom',display_name:'自定义提供方',api_type:'openai_chat_completions',base_url:'',auth_type:'api_key',models:[]},...connections.map(connection=>({...connection,models:[]})), ...Array.from({length:15}, (_, index) => ({provider_id:`test-${index}`,display_name:`Provider ${index}`,api_type:'openai_chat_completions',base_url:'https://example.invalid',auth_type:'api_key',models:[]}))] }
    else if (path === '/api/models/configurations/test') { const body = route.request().postDataJSON(); data = {kind:body.kind,outcome:'success',elapsed_ms:125,code:body.kind==='basic'?'model_listed':'text_received',text:body.kind==='basic'?null:'OK',image:null} }
    else if (path.endsWith('/models')) data = {outcome:'success',code:'models_received',items:[{model_name:'qwen3:14b',display_name:'Qwen3 14B',image_support:'unknown'},{model_name:'qwen3:8b',display_name:'Qwen3 8B',image_support:'unknown'}]}
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
  test(`提供方模型设置 ${language} ${theme}`, async ({page}, testInfo) => {
    const failures: string[] = []
    page.on('pageerror', error => failures.push(error.message))
    await setup(page)
    await openSettings(page, language, theme)
    const dialog = page.getByRole('dialog', {name:language==='en'?'Settings':'设置',exact:true})
    for (const width of [320,768,1024,1440]) {
      await page.setViewportSize({width,height:960})
      await expect(dialog.getByRole('heading',{name:'DeepSeek',exact:true})).toBeVisible()
      await expect(dialog.getByRole('button',{name:language==='en'?'Configure DeepSeek V4 Pro':'配置模型 DeepSeek V4 Pro'})).toBeVisible()
      const bounds=await dialog.evaluate(element=>({scroll:element.scrollWidth,width:element.clientWidth}))
      expect(bounds.scroll).toBeLessThanOrEqual(bounds.width+1)
      await page.screenshot({path:testInfo.outputPath(`providers-${width}.png`),animations:'disabled'})
      const search = dialog.getByRole('textbox', {name:language==='en'?'Search providers or models':'搜索提供方或模型'})
      await search.fill(' FLASH ')
      const list = dialog.getByRole('region', {name:language==='en'?'Model list':'模型列表',exact:true})
      await expect(list.getByText('DeepSeek V4 Flash', {exact:true})).toBeVisible()
      await expect(list.getByText('DeepSeek V4 Pro', {exact:true})).toHaveCount(0)
      await page.screenshot({path:testInfo.outputPath(`search-${width}.png`),animations:'disabled'})
      await search.fill('qwen3:14b')
      await expect(dialog.getByRole('heading', {name:'本地 Ollama',exact:true})).toBeVisible()
      await search.fill('missing-model')
      await expect(dialog.getByText(language==='en'?'No matching providers or models':'没有匹配的提供方或模型')).toBeVisible()
      await dialog.getByRole('button', {name:language==='en'?'Clear search':'清除搜索'}).click()
      await expect(dialog.getByRole('heading', {name:'DeepSeek',exact:true})).toBeVisible()
    }
    await dialog.getByRole('button',{name:language==='en'?'Connection settings':'连接设置',exact:true}).click()
    await expect(dialog.getByLabel('API Key', { exact: true })).toHaveValue('')
    await dialog.getByRole('navigation',{name:language==='en'?'Model settings navigation':'模型配置导航'}).getByRole('button',{name:language==='en'?'Models':'模型配置',exact:true}).click()
    await dialog.getByRole('button',{name:/本地 Ollama/}).click()
    await expect(dialog.getByText(language==='en'?'No authentication':'无需认证',{exact:true})).toBeVisible()
    await dialog.getByRole('button',{name:language==='en'?'Fetch models':'获取模型',exact:true}).click()
    await expect(dialog.getByRole('checkbox',{name:'qwen3:8b',exact:true})).toBeVisible()
    await dialog.getByRole('checkbox',{name:'qwen3:8b',exact:true}).check()
    for (const width of [320,768,1024,1440]) {
      await page.setViewportSize({width,height:960})
      const search = dialog.getByRole('textbox', {name:language==='en'?'Search model names or Model IDs':'搜索模型名称或 Model ID'})
      await search.fill('Qwen3 8B')
      await expect(dialog.getByRole('checkbox',{name:'qwen3:8b',exact:true})).toBeChecked()
      await expect(dialog.getByRole('checkbox',{name:/qwen3:14b/})).toHaveCount(0)
      await expect(dialog.getByText(language==='en'?'1 selected':'已选 1 项', {exact:true})).toBeVisible()
      const bounds = await dialog.evaluate(element=>({scroll:element.scrollWidth,width:element.clientWidth}))
      expect(bounds.scroll).toBeLessThanOrEqual(bounds.width+1)
      await page.screenshot({path:testInfo.outputPath(`discovery-search-${width}.png`),animations:'disabled'})
    }
    await dialog.getByRole('navigation',{name:language==='en'?'Model settings navigation':'模型配置导航'}).getByRole('button',{name:language==='en'?'Models':'模型配置',exact:true}).click()
    await dialog.getByRole('button',{name:language==='en'?'Configure Qwen3 14B':'配置模型 Qwen3 14B'}).click()
    await expect(dialog.getByLabel('Model ID',{exact:true})).toHaveValue('qwen3:14b')
    await dialog.getByRole('button',{name:language==='en'?'Text reply':'文字回复'}).click()
    await expect(dialog.getByText('OK',{exact:true})).toBeVisible()
    await page.screenshot({path:testInfo.outputPath('model-form.png'),animations:'disabled'})
    expect(failures).toEqual([])
  })
}


test('固定工具栏、列表独立滚动、分栏拖动及设置尺寸一致', async ({page}) => {
  await setup(page, true)
  await openSettings(page, 'zh-CN', 'light')
  const dialog = page.getByRole('dialog', {name:'设置', exact:true})
  const add = dialog.getByRole('button', {name:'添加提供方', exact:true})
  const original = await dialog.boundingBox()
  for (const name of ['账号管理', '通用', '模型配置']) {
    await dialog.getByRole('button', {name, exact:true}).click()
    await expect.poll(() => dialog.boundingBox()).toEqual(original)
  }
  const splitter = dialog.getByRole('separator', {name:'调整提供方列表宽度'})
  const bounds = await splitter.boundingBox()
  const initialWidth = Number(await splitter.getAttribute('aria-valuenow'))
  await page.mouse.move(bounds!.x + bounds!.width / 2, bounds!.y + 80)
  await page.mouse.down()
  await page.mouse.move(bounds!.x + bounds!.width / 2 + 60, bounds!.y + 80)
  await page.mouse.up()
  expect(Number(await splitter.getAttribute('aria-valuenow'))).toBeGreaterThan(initialWidth)
  await splitter.press('Home')
  expect(await splitter.getAttribute('aria-valuenow')).toBe(await splitter.getAttribute('aria-valuemin'))
  await splitter.press('End')
  expect(await splitter.getAttribute('aria-valuenow')).toBe(await splitter.getAttribute('aria-valuemax'))
  const list = dialog.getByRole('region', {name:'模型列表', exact:true})
  const addBounds = await add.boundingBox()
  const heading = dialog.getByRole('heading', {name:'DeepSeek', exact:true})
  const headingBounds = await heading.boundingBox()
  await list.hover()
  await page.mouse.wheel(0, 900)
  await expect.poll(() => list.evaluate(element => element.scrollTop)).toBeGreaterThan(0)
  expect(await add.boundingBox()).toEqual(addBounds)
  expect(await heading.boundingBox()).toEqual(headingBounds)
  await add.click()
  const form = dialog.getByRole('region', {name:'模型配置', exact:true})
  expect(await form.evaluate(element => element.scrollTop)).toBe(0)
  await dialog.getByRole('button', {name:'提供方', exact:true}).click()
  const options = dialog.getByRole('listbox', {name:'提供方', exact:true})
  expect(await options.evaluate(element => element.scrollTop)).toBe(0)
  expect((await options.boundingBox())!.height).toBeLessThanOrEqual(200)
  expect((await options.getByRole('option').first().boundingBox())!.height).toBeLessThanOrEqual(40)
  const formTop = await form.evaluate(element => element.scrollTop)
  await options.press('End')
  await expect.poll(() => options.evaluate(element => element.scrollTop)).toBeGreaterThan(0)
  expect(await form.evaluate(element => element.scrollTop)).toBe(formTop)
  await options.press('Escape')
  const back = dialog.getByRole('navigation',{name:'模型配置导航'}).getByRole('button',{name:'模型配置',exact:true})
  await page.setViewportSize({width:1440,height:600})
  const backBounds = await back.boundingBox()
  await form.hover()
  await page.mouse.wheel(0, 900)
  await expect.poll(() => form.evaluate(element => element.scrollTop)).toBeGreaterThan(0)
  expect(await back.boundingBox()).toEqual(backBounds)
  await back.click()
  await add.click()
  expect(await dialog.getByRole('region', {name:'模型配置', exact:true}).evaluate(element => element.scrollTop)).toBe(0)
})

test('展开生成参数保留字段与边框之间的内边距', async ({page}) => {
  await setup(page)
  await openSettings(page, 'zh-CN', 'light')
  await page.getByRole('button', {name:'配置模型 DeepSeek V4 Pro'}).click()
  const parameters = page.locator('details').filter({has:page.getByText('生成参数', {exact:true})})
  await parameters.locator('summary').click()
  const box = await parameters.boundingBox()
  const label = await parameters.locator('label').filter({hasText:'最大输出 Token'}).boundingBox()
  const helper = await parameters.getByText('用逗号分隔，最多四项').boundingBox()
  expect(label!.x - box!.x).toBeGreaterThanOrEqual(12)
  expect(box!.y + box!.height - helper!.y - helper!.height).toBeGreaterThanOrEqual(12)
})

test('取消模型测试显示全局 warning Toast', async ({page}) => {
  let release!: () => void
  const responseGate = new Promise<void>(resolve => { release = resolve })
  await setup(page)
  await page.route('**/api/models/configurations/test', async route => {
    await responseGate
    try {
      await route.fulfill({json:{code:0,message:'success',data:{kind:'text',outcome:'success',elapsed_ms:125,code:'text_received',text:'OK',image:null}}})
    } catch {
      // 取消测试会中止浏览器请求，路由可能已经无法返回响应
    }
  })
  await openSettings(page, 'zh-CN', 'light')
  const dialog = page.getByRole('dialog', {name:'设置', exact:true})
  await dialog.getByRole('button', {name:'配置模型 DeepSeek V4 Pro', exact:true}).click()
  await dialog.getByRole('button', {name:'文字回复', exact:true}).click()
  const pending = dialog.getByRole('status').filter({hasText:'测试进行中'})
  await expect(pending).toBeVisible()
  await pending.getByRole('button', {name:'取消等待', exact:true}).click()
  await expect(page.locator('.toast-card.is-warning')).toContainText('已取消等待，服务商可能仍在处理并计费')
  release()
})

for (const language of ['zh-CN', 'en'] as const) for (const theme of ['light', 'dark'] as const) {
  test(`参数、错误和固定操作样式 ${language} ${theme}`, async ({page}, testInfo) => {
    await setup(page)
    await openSettings(page, language, theme)
    const en = language === 'en'
    const dialog = page.getByRole('dialog', {name:en?'Settings':'设置', exact:true})
    await dialog.getByRole('button', {name:en?'Configure DeepSeek V4 Pro':'配置模型 DeepSeek V4 Pro'}).click()
    const parameters = dialog.locator('details').filter({has:page.getByText(en?'Generation parameters':'生成参数', {exact:true})})
    await parameters.locator('summary').click()
    for (const width of [320,768,1024,1440]) {
      await page.setViewportSize({width,height:960})
      await parameters.scrollIntoViewIfNeeded()
      const box = (await parameters.boundingBox())!
      const labels = await parameters.locator('label').all()
      for (const label of labels) {
        const bounds = (await label.boundingBox())!
        expect(bounds.x - box.x).toBeGreaterThanOrEqual(12)
        expect(bounds.x + bounds.width).toBeLessThanOrEqual(box.x + box.width - 12)
      }
      const form = dialog.getByRole('region', {name:en?'Models':'模型配置', exact:true})
      expect(await form.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true)
      const save = dialog.getByRole('button', {name:en?'Save':'保存', exact:true})
      expect(await form.evaluate((element, button) => element.contains(button), await save.elementHandle())).toBe(false)
      const saveBounds = (await save.boundingBox())!
      const closeBounds = (await dialog.getByRole('button', {name:en?'Close dialog':'关闭对话框',exact:true}).boundingBox())!
      const dialogBounds = (await dialog.boundingBox())!
      expect(Math.abs(saveBounds.x + saveBounds.width - closeBounds.x - closeBounds.width)).toBeLessThanOrEqual(1)
      expect(dialogBounds.y + dialogBounds.height - saveBounds.y - saveBounds.height).toBeLessThanOrEqual(14)
      await page.screenshot({path:testInfo.outputPath(`parameters-${width}.png`),animations:'disabled'})
    }
    const stop = dialog.getByLabel(en?'Stop sequences':'停止序列', {exact:true})
    await stop.fill('a,b,c,d,e')
    await dialog.getByRole('button', {name:en?'Save':'保存', exact:true}).click()
    await expect(stop).toHaveAttribute('aria-invalid','true')
    await page.screenshot({path:testInfo.outputPath('parameter-error.png'),animations:'disabled'})
    await stop.fill('')
    await dialog.getByRole('radio', {name:en?'Image generation':'图片生成', exact:true}).check()
    const advanced = dialog.locator('details').filter({has:page.getByText(en?'Advanced parameters':'高级参数', {exact:true})})
    await advanced.locator('summary').click()
    const editor = dialog.getByRole('textbox', {name:en?'Advanced parameters JSON':'高级参数 JSON', exact:true})
    await expect(editor).toBeVisible()
    await editor.fill('{')
    await expect(editor).toHaveAttribute('aria-invalid','true')
    for (const width of [320,1440]) {
      await page.setViewportSize({width,height:960})
      await advanced.scrollIntoViewIfNeeded()
      const box = (await advanced.boundingBox())!
      const text = (await advanced.locator('p').first().boundingBox())!
      expect(text.x - box.x).toBeGreaterThanOrEqual(12)
      await page.screenshot({path:testInfo.outputPath(`image-error-${width}.png`),animations:'disabled'})
    }
    await dialog.getByRole('navigation',{name:en?'Model settings navigation':'模型配置导航'}).getByRole('button',{name:en?'Models':'模型配置',exact:true}).click()
    await dialog.getByRole('button', {name:en?'Connection settings':'连接设置',exact:true}).click()
    const form = dialog.getByRole('region', {name:en?'Models':'模型配置',exact:true})
    const deletion = dialog.getByRole('button', {name:en?'Delete provider':'删除提供方',exact:true})
    const save = dialog.getByRole('button', {name:en?'Save':'保存',exact:true})
    for (const button of [deletion,save]) expect(await form.evaluate((element,target) => element.contains(target),await button.elementHandle())).toBe(false)
    const footer = await deletion.boundingBox()
    await form.hover()
    await page.mouse.wheel(0,900)
    expect(await deletion.boundingBox()).toEqual(footer)
    await deletion.click()
    await expect(dialog.getByRole('alert')).toBeVisible()
    await expect(save).toHaveCount(0)
    await page.screenshot({path:testInfo.outputPath('delete-confirmation.png'),animations:'disabled'})
    await dialog.getByRole('button',{name:en?'Cancel':'取消',exact:true}).click()
    await expect(save).toBeVisible()
  })
}

test('列表未溢出时保持静止且不产生滚动回弹', async ({page}) => {
  await setup(page)
  await openSettings(page,'zh-CN','light')
  const list = page.getByRole('region',{name:'模型列表',exact:true})
  expect(await list.evaluate(element => element.scrollHeight <= element.clientHeight)).toBe(true)
  await expect(list).toHaveCSS('overscroll-behavior-y','none')
  const before = await list.getByRole('button',{name:'配置模型 DeepSeek V4 Pro',exact:true}).boundingBox()
  await list.hover()
  await page.mouse.wheel(0,500)
  await page.mouse.wheel(0,-500)
  expect(await list.evaluate(element => element.scrollTop)).toBe(0)
  expect(await list.getByRole('button',{name:'配置模型 DeepSeek V4 Pro',exact:true}).boundingBox()).toEqual(before)
})

test.describe('模型配置触控', () => {
  test.use({hasTouch:true,viewport:{width:320,height:960}})
  test('表单和操作保持可触控尺寸',async ({page},testInfo) => {
    await page.emulateMedia({reducedMotion:'reduce'})
    await setup(page)
    await openSettings(page,'zh-CN','light')
    const dialog = page.getByRole('dialog',{name:'设置',exact:true})
    await dialog.getByRole('button',{name:'添加提供方',exact:true}).click()
    const provider = dialog.getByRole('button',{name:'提供方',exact:true})
    expect((await provider.boundingBox())!.height).toBeGreaterThanOrEqual(44)
    await provider.click()
    const menu = dialog.getByRole('listbox',{name:'提供方',exact:true})
    for (const option of await menu.getByRole('option').all()) expect((await option.boundingBox())!.height).toBeGreaterThanOrEqual(44)
    await menu.press('Escape')
    await dialog.getByRole('button',{name:'保存',exact:true}).click()
    await expect(dialog.getByLabel('显示名称',{exact:true})).toHaveAttribute('aria-invalid','true')
    for (const name of ['保存','取消']) expect((await dialog.getByRole('button',{name,exact:true}).boundingBox())!.height).toBeGreaterThanOrEqual(44)
    expect((await dialog.getByRole('navigation',{name:'模型配置导航'}).getByRole('button',{name:'模型配置',exact:true}).boundingBox())!.height).toBeGreaterThanOrEqual(44)
    await page.screenshot({path:testInfo.outputPath('touch-errors-320.png'),animations:'disabled'})
  })
})

for (const theme of ['light','dark'] as const) test(`语言选择与外观选项等宽左对齐 ${theme}`, async ({page},testInfo) => {
  await setup(page)
  await openSettings(page,'zh-CN',theme)
  const dialog = page.getByRole('dialog',{name:'设置',exact:true})
  await dialog.getByRole('button',{name:'通用',exact:true}).click()
  for (const width of [320,768,1024,1440]) {
    await page.setViewportSize({width,height:960})
    const appearance = page.locator('label').filter({has:page.getByRole('radio',{name:'跟随系统',exact:true})})
    const language = dialog.getByRole('button',{name:'界面语言',exact:true})
    const appearanceBox = (await appearance.boundingBox())!
    const languageBox = (await language.boundingBox())!
    expect(Math.abs(appearanceBox.width-languageBox.width)).toBeLessThanOrEqual(1)
    const label = (await language.getByText('简体中文',{exact:true}).boundingBox())!
    expect(label.x-languageBox.x).toBeGreaterThanOrEqual(12)
    expect(label.x-languageBox.x).toBeLessThanOrEqual(14)
    await language.click()
    await expect(dialog.getByRole('listbox',{name:'界面语言',exact:true})).toBeVisible()
    await dialog.getByRole('listbox',{name:'界面语言',exact:true}).press('Escape')
    await page.screenshot({path:testInfo.outputPath(`language-${width}.png`),animations:'disabled'})
  }
})
