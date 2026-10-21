import { beforeEach, expect, it } from 'vitest'
import { convertComputerResources } from './convertComputerResources'

beforeEach(() => { localStorage.clear(); sessionStorage.clear() })

it('清理执行位置时保留会话请求、确认选择、附件和草稿', () => {
  const obsolete = JSON.stringify(['tinkerfin.execution-location', 'https://studio.test', 7, 'project'])
  localStorage.setItem(obsolete, JSON.stringify({ device_id: 'device', workspace_id: 'project' }))
  localStorage.setItem('draft', '尚未发送的草稿')
  const current = { serverAddress: 'https://studio.test', userId: 7, runs: [{ threadId: 'thread', projectId: 'project', mode: 'start', lastSeq: 12,
    payload: { threadId: 'thread', runId: 'original', messages: [{ id: 'input', role: 'user', content: '保存报告' }],
      forwardedProps: { projectId: 'project', model: 'main', accessMode: 'full', command: { plan: 'off' }, skillIds: ['skill'] } } }] }
  const saved = structuredClone(current)
  Object.assign(saved.runs[0].payload.forwardedProps, { localTarget: { device_id: 'device', workspace_id: 'project' }, local_target: null })
  sessionStorage.setItem('tinkerfin:active-conversation-run', JSON.stringify(saved))
  convertComputerResources()
  expect(localStorage.getItem(obsolete)).toBeNull()
  expect(localStorage.getItem('draft')).toBe('尚未发送的草稿')
  expect(JSON.parse(sessionStorage.getItem('tinkerfin:active-conversation-run')!)).toEqual(current)
  convertComputerResources()
  expect(JSON.parse(sessionStorage.getItem('tinkerfin:active-conversation-run')!)).toEqual(current)
})

it('其他标签页已清理偏好时仍转换当前标签页的恢复缓存', () => {
  localStorage.setItem('tinkerfin:execution-location-removed', 'true')
  sessionStorage.setItem('tinkerfin:active-conversation-run', JSON.stringify({ runs: [{ payload: { forwardedProps: { localTarget: {}, accessMode: 'write_approval' } } }] }))
  convertComputerResources()
  expect(JSON.parse(sessionStorage.getItem('tinkerfin:active-conversation-run')!)).toEqual({ runs: [{ payload: { forwardedProps: { accessMode: 'write_approval' } } }] })
})

it('异常缓存不会阻止启动或改写其他存储', () => {
  sessionStorage.setItem('tinkerfin:active-conversation-run', '{')
  sessionStorage.setItem('draft', '草稿')
  expect(convertComputerResources).not.toThrow()
  expect(sessionStorage.getItem('tinkerfin:active-conversation-run')).toBe('{')
  expect(sessionStorage.getItem('draft')).toBe('草稿')
})
