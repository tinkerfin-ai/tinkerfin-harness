import { expect, test } from '@playwright/test'
import fixture from './fixtures/multimodal-history.json' with { type: 'json' }

const user = {user_id:1,username:'scroll-test',display_name:'滚动验收',avatar_url:null,roles:[],disabled:false}
const history = {
  ...fixture, title:'滚动边界验收', lastModel:'model-0', messageCount:2, toolCallCount:0,
  messages:[
    {...fixture.messages[0],content:'检查滚动区域'},
    {...fixture.messages[0],id:'scroll-reply',sourceId:'scroll-reply',traceSeq:6,role:'assistant',content:Array.from({length:50},(_,i)=>`第 ${i+1} 段：用于核验菜单、输入框和会话各自的滚动边界。`).join('\n\n')},
  ],
  reasoning:[],interactions:[],runFailures:[],
  graph:{...fixture.graph,turns:[],nodes:[],orderedNodeIds:[],matchedNodeIds:[]},
  taskTrace:{status:'ready',todoGroups:[]},
}

for (const theme of ['light','dark']) test(`输入框及菜单滚动不移动会话 ${theme}`, async ({page}) => {
  await page.addInitScript(({user,theme}) => {
    localStorage.setItem('tinkerfin.auth.session',JSON.stringify({token:'scroll-test-token',tokenType:'Bearer',expiresAt:'2099-01-01T00:00:00Z',user}))
    localStorage.setItem('tinkerfin:theme',theme)
    localStorage.setItem('tinkerfin:language','zh-CN')
  },{user,theme})
  await page.route('**/api/**',async route => {
    const path = new URL(route.request().url()).pathname
    let data:unknown = {}
    if (path === '/api/auth/me') data = {expires_at:'2099-01-01T00:00:00Z',user}
    else if (path === '/api/models') data = {defaultModelId:'model-0',items:Array.from({length:35},(_,i)=>({modelId:`model-${i}`,displayName:`Model ${i}`,imageSupport:'unknown',reasoningEnabled:false,isDefault:i===0}))}
    else if (path === '/api/conversation/config') data = {dayRanges:[7,30]}
    else if (path === '/api/conversation/history') data = {items:[{...history,status:'idle',hasPendingInterrupt:false,updatedAt:'2026-09-14T00:00:00Z'}],nextCursor:null}
    else if (path.endsWith(`/${history.threadId}/history`)) data = history
    else if (path.endsWith(`/${history.threadId}/trace`)) {
      await route.fulfill({contentType:'text/event-stream',body:`event: trace\ndata: ${JSON.stringify({type:'snapshot',snapshot:history})}\n\n`})
      return
    }
    await route.fulfill({json:{code:0,message:'success',data}})
  })
  await page.goto('/')
  await page.getByRole('button',{name:'打开会话：滚动边界验收',exact:true}).click()
  const pane = page.getByRole('region',{name:'对话内容',exact:true})
  await expect(page.getByText('检查滚动区域',{exact:true})).toBeAttached()
  await expect.poll(() => pane.evaluate(element => element.scrollHeight-element.clientHeight)).toBeGreaterThan(1000)
  await expect.poll(() => pane.evaluate(element => element.scrollTop)).toBeGreaterThan(1000)
  await pane.hover({position:{x:80,y:80}})
  // 先确认监听已注册，再触发用户滚动；向上滚动同时解除会话的自动跟随
  const scroll = await pane.evaluateHandle(element => {
    const initialTop = element.scrollTop
    return { settled: new Promise<void>(resolve => {
      const onEnd = () => {
        if (element.scrollTop >= initialTop) return
        element.removeEventListener('scrollend', onEnd)
        resolve()
      }
      element.addEventListener('scrollend', onEnd)
    }) }
  })
  try {
    await page.mouse.wheel(0,-600)
    await scroll.evaluate(async state => { await state.settled })
  } finally { await scroll.dispose() }
  const initialScroll = await pane.evaluate(element=>element.scrollTop)
  const assertConversationUnchanged = async () => expect(await pane.evaluate(element=>element.scrollTop)).toBe(initialScroll)
  const select = page.getByRole('button',{name:'选择模型',exact:true})
  await select.click()
  const models = page.getByRole('listbox',{name:'模型选项',exact:true})
  await models.hover()
  await page.mouse.wheel(0,200)
  await expect.poll(() => models.evaluate(element=>element.scrollTop)).toBeGreaterThan(0)
  await assertConversationUnchanged()
  await models.evaluate(element=>{element.scrollTop=element.scrollHeight})
  await page.mouse.wheel(0,600)
  await models.press('End')
  await assertConversationUnchanged()
  await models.evaluate(element=>{element.scrollTop=0})
  await page.mouse.wheel(0,-600)
  await models.press('Home')
  await assertConversationUnchanged()
  await models.press('Escape')
  await page.getByRole('button',{name:'选择访问权限',exact:true}).click()
  const access = page.getByRole('listbox',{name:'访问权限选项',exact:true})
  await access.hover()
  await page.mouse.wheel(0,-500)
  await page.mouse.wheel(0,500)
  await access.press('ArrowDown')
  await assertConversationUnchanged()
  await access.press('Escape')
  const input = page.getByRole('textbox',{name:'消息输入',exact:true})
  await input.hover()
  await page.mouse.wheel(0,-500)
  await assertConversationUnchanged()
  await input.fill(Array.from({length:40},(_,i)=>`输入行 ${i}`).join('\n'))
  const inputScroll = page.locator('.composer-input-scroll')
  await inputScroll.evaluate(element=>{element.scrollTop=0})
  await inputScroll.hover()
  await page.mouse.wheel(0,200)
  await expect.poll(() => inputScroll.evaluate(element=>element.scrollTop)).toBeGreaterThan(0)
  await assertConversationUnchanged()
  await pane.hover({position:{x:80,y:80}})
  await page.mouse.wheel(0,300)
  await expect.poll(() => pane.evaluate(element=>element.scrollTop)).toBeGreaterThan(initialScroll)
})
