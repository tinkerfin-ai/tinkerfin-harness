import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { App } from '../src/App'
import type { ActionResult, DesktopAction, DesktopState, UpdatePhase } from '../src/state'
import { fixture } from './fixture'

let revision = 0
function publish(state: DesktopState) { window.receiveDesktopState({ ...state, revision: ++revision }) }
async function mount(patch: Partial<DesktopState> = {}, respond: (action: DesktopAction) => Promise<ActionResult> = async () => ({ ok: true })) {
  const state = fixture(patch)
  const calls = vi.fn(respond)
  window.webkit = { messageHandlers: { desktop: { postMessage: calls } } }
  await act(async () => { publish(state); render(<App />) })
  return { state, calls }
}
async function click(name: string) { await act(async () => { fireEvent.click(screen.getByRole('button', { name })) }) }

describe('桌面真实动作边界', () => {
  it.each([
    ['network', '请检查网络后重试'], ['storage', '请检查磁盘空间和目录权限'],
    ['release', '更新文件不可用，请稍后重试'], ['unavailable', '请重试'],
  ] as const)('下载失败按 %s 原因提供恢复操作', async (failure, message) => {
    await mount({ update: { ...fixture().update, phase: 'download-error', failure } })
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByText(message, { exact: true })).toBeInTheDocument()
    if (failure !== 'network') expect(within(dialog).queryByText('请检查网络后重试')).not.toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: /^重试$/ })).toBeEnabled()
  })

  it('本机配置不可读时保留同一连接页的可操作重试入口', async () => {
    const { calls } = await mount({ ready: false, connected: false, connection: 'offline', service: '', api: '', connectionError: '请检查本机配置后重试' })
    expect(screen.getByText('请检查本机配置后重试')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重新连接' })).toBeEnabled()
    await click('重新连接')
    expect(calls).toHaveBeenCalledWith({ type: 'retry' })
  })

  it('待授权失效回到连接表单后只保留一条原因', async () => {
    const { state } = await mount({ connected: false, connection: 'unpaired', connectionError: '授权已失效，请重新连接' })
    expect(screen.getByLabelText('服务地址')).toBeInTheDocument()
    expect(screen.getAllByText('授权已失效，请重新连接')).toHaveLength(1)
    await act(async () => publish({ ...state, connection: 'revoked' }))
    expect(screen.getAllByText('授权已失效，请重新连接')).toHaveLength(1)
  })

  it('未连接时只允许连接页，地址校验不发起原生请求', async () => {
    const { calls } = await mount({ connected: false, connection: 'unpaired', service: '', api: '' })
    for (const name of ['工作区', '权限', '我的']) expect(screen.getByRole('button', { name })).toBeDisabled()
    await click('连接并授权')
    expect(screen.getByLabelText('服务地址')).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByLabelText('服务地址')).toHaveFocus()
    expect(calls.mock.calls.some(([action]) => action.type === 'connect')).toBe(false)
  })

  it('API识别失败展开输入，保留服务地址并用同一提交入口重试', async () => {
    const { calls } = await mount({ connected: false, connection: 'unpaired', service: '', api: '' }, async action => action.type === 'connect' ? { ok: false, field: 'api', message: '无法识别 API 地址，请填写' } : { ok: true })
    fireEvent.change(screen.getByLabelText('服务地址'), { target: { value: 'https://next.example.com' } })
    await click('连接并授权')
    expect(screen.getByLabelText('服务地址')).toHaveValue('https://next.example.com')
    expect(screen.getByLabelText('API 地址')).toHaveAttribute('aria-invalid', 'true')
    expect(calls).toHaveBeenCalledWith({ type: 'connect', service: 'https://next.example.com', api: '' })
  })

  it('名称只通过铅笔编辑，同页保留草稿，切换菜单取消编辑', async () => {
    const { calls } = await mount()
    await click('我的')
    expect(screen.queryByLabelText('设备名称')).not.toBeInTheDocument()
    await click('编辑设备名称')
    const input = screen.getByLabelText('设备名称')
    fireEvent.change(input, { target: { value: '未提交的名称' } }); fireEvent.blur(input)
    await click('语言：简体中文')
    fireEvent.keyDown(screen.getByRole('listbox'), { key: 'Escape' })
    expect(screen.getByLabelText('设备名称')).toHaveValue('未提交的名称')
    expect(calls.mock.calls.some(([action]) => action.type === 'save-name')).toBe(false)
    await click('权限'); await click('我的')
    expect(screen.queryByLabelText('设备名称')).not.toBeInTheDocument()
    await click('编辑设备名称')
    expect(screen.getByLabelText('设备名称')).toHaveValue('研究用 Mac')
    fireEvent.keyDown(screen.getByLabelText('设备名称'), { key: 'Escape' })
    expect(screen.getByRole('button', { name: '编辑设备名称' })).toHaveFocus()
    expect(screen.queryByLabelText('设备名称')).not.toBeInTheDocument()
  })

  it('名称保存中切菜单，迟到结果只更新保存值并保持只读', async () => {
    let finish!: (result: ActionResult) => void
    const saving = new Promise<ActionResult>(resolve => { finish = resolve })
    const { state, calls } = await mount({}, action => action.type === 'save-name' ? saving : Promise.resolve({ ok: true }))
    await click('我的'); await click('编辑设备名称')
    fireEvent.change(screen.getByLabelText('设备名称'), { target: { value: '已提交的名称' } })
    await click('保存设备名称')
    await click('权限'); await click('我的')
    expect(screen.queryByLabelText('设备名称')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '编辑设备名称' })).toBeDisabled()
    await act(async () => { publish({ ...state, name: '已提交的名称' }); finish({ ok: true }) })
    expect(screen.queryByLabelText('设备名称')).not.toBeInTheDocument()
    expect(screen.getByText('已提交的名称')).toBeVisible()
    expect(calls).toHaveBeenCalledWith({ type: 'save-name', name: '已提交的名称' })
  })

  it.each(['authorization', 'retry'] as const)('首次授权采用识别后的地址，%s 断线恢复保留切换草稿', async connectionReason => {
    const { state } = await mount({ connected: false, connection: 'unpaired', service: '', api: '' })
    fireEvent.change(screen.getByLabelText('服务地址'), { target: { value: 'https://studio.example.com' } })
    await click('连接并授权')
    const online: DesktopState = { ...state, connected: true, connection: 'online', connectionReason: 'authorization', service: 'https://studio.example.com', api: 'https://api.example.com' }
    await act(async () => publish(online))
    await click('连接')
    expect(screen.getByText('自动识别')).toBeVisible()
    expect(screen.queryByLabelText('API 地址')).not.toBeInTheDocument()
    expect(screen.queryByText('新授权成功后将更换连接，本机执行会中断')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '打开 Studio' })).toBeEnabled()
    await click('手动填写')
    expect(screen.getByLabelText('API 地址')).toHaveValue(online.api)
    await click('恢复自动识别')
    expect(screen.getByRole('button', { name: '打开 Studio' })).toBeEnabled()
    fireEvent.change(screen.getByLabelText('服务地址'), { target: { value: 'https://next.example.com' } })
    await act(async () => publish({ ...online, connected: false, connection: 'offline' }))
    await act(async () => publish({ ...online, connectionReason }))
    await click('连接')
    expect(screen.getByLabelText('服务地址')).toHaveValue('https://next.example.com')
    expect(screen.getByText('新授权成功后将更换连接，本机执行会中断')).toBeVisible()
  })

  it('菜单栏浮层保留按钮名称且没有悬浮提示', async () => {
    await mount({ surface: 'status' })
    for (const label of ['设置', '退出']) {
      const button = screen.getByRole('button', { name: label })
      fireEvent.mouseEnter(button); fireEvent.focus(button)
      expect(button).toHaveAccessibleName(label)
      expect(button).not.toHaveAttribute('title')
    }
    expect(screen.getByText('研究用 Mac')).not.toHaveAttribute('title')
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })

  it('已有独立API地址默认自动识别，取消编辑及退出登录恢复自动模式', async () => {
    const { state } = await mount({ api: 'https://api.example.com' })
    await click('连接')
    expect(screen.getByText('自动识别')).toBeVisible()
    expect(screen.queryByLabelText('API 地址')).not.toBeInTheDocument()
    await click('手动填写')
    expect(screen.getByLabelText('API 地址')).toHaveValue(state.api)
    fireEvent.change(screen.getByLabelText('API 地址'), { target: { value: 'https://draft.example.com' } })
    await click('取消')
    expect(screen.getByText('自动识别')).toBeVisible()
    await click('手动填写')
    fireEvent.change(screen.getByLabelText('API 地址'), { target: { value: 'https://draft.example.com' } })
    fireEvent.keyDown(screen.getByLabelText('API 地址'), { key: 'Escape' })
    expect(screen.getByText('自动识别')).toBeVisible()
    await click('手动填写')
    await click('我的'); await click('退出登录')
    act(() => publish({ ...state, connected: false, connection: 'unpaired' }))
    expect(screen.getByText('自动识别')).toBeVisible()
    expect(screen.queryByLabelText('API 地址')).not.toBeInTheDocument()
  })

  it('名称保存失败保留草稿与同一勾选入口，错误说明与字段关联', async () => {
    const { calls } = await mount({}, async action => action.type === 'save-name' ? { ok: false, field: 'name', message: '保存失败，请重试' } : { ok: true })
    await click('我的'); await click('编辑设备名称')
    fireEvent.change(screen.getByLabelText('设备名称'), { target: { value: '新名称' } })
    await click('保存设备名称')
    expect(screen.getByLabelText('设备名称')).toHaveValue('新名称')
    expect(screen.getByLabelText('设备名称')).toHaveAccessibleDescription('保存失败，请重试')
    expect(screen.getByRole('button', { name: '保存设备名称' })).toBeEnabled()
    expect(calls).toHaveBeenCalledWith({ type: 'save-name', name: '新名称' })
  })

  it('刷新和目录打开失败保留项目清单，退出登录失败保留草稿且无提示条', async () => {
    await mount({}, async action => ['refresh-projects', 'open-project', 'logout'].includes(action.type) ? { ok: false, field: '', message: '请重试' } : { ok: true })
    await click('刷新'); await click('量化研究 · 在 Finder 中打开')
    expect(screen.getByRole('button', { name: '市场报告 · 在 Finder 中打开' })).toBeEnabled()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    await click('我的'); await click('编辑设备名称')
    fireEvent.change(screen.getByLabelText('设备名称'), { target: { value: '保留草稿' } })
    await click('退出登录')
    expect(screen.getByLabelText('设备名称')).toHaveValue('保留草稿')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '退出登录' })).toBeEnabled()
  })

  it('系统权限撤回只更新对应行，掉线禁用其他页面', async () => {
    const { state } = await mount()
    await click('权限')
    expect(screen.getByRole('button', { name: '已开启' })).toBeInTheDocument()
    act(() => publish({ ...state, permissions: { accessibility: false, screenCapture: false } }))
    expect(screen.getAllByRole('button', { name: '前往开启' })).toHaveLength(2)
    act(() => publish({ ...state, connected: false, connection: 'offline' }))
    expect(screen.getByRole('button', { name: '我的' })).toBeDisabled()
    expect(screen.getByText('连接已中断')).toBeInTheDocument()
  })

  it('取消授权后返回连接表单并保留输入', async () => {
    const { state, calls } = await mount({ connected: false, connection: 'authorizing', authorizationPhase: 'waiting' })
    await click('取消授权')
    expect(calls).toHaveBeenCalledWith({ type: 'cancel-authorization' })
    act(() => publish({ ...state, connection: 'unpaired', authorizationPhase: 'idle' }))
    expect(screen.getByLabelText('服务地址')).toHaveValue(state.service)
  })

  it('掉线关闭语言菜单，重新连接后回到工作区', async () => {
    const { state } = await mount()
    await click('我的'); await click('语言：简体中文')
    expect(screen.getByRole('listbox')).toBeInTheDocument()
    act(() => publish({ ...state, connected: false, connection: 'offline' }))
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    act(() => publish(state))
    expect(screen.getByRole('button', { name: '工作区' })).toHaveAttribute('aria-current', 'page')
  })

  it('输入法Escape不取消草稿，退出登录成功才清除未提交编辑', async () => {
    await mount()
    await click('我的'); await click('编辑设备名称')
    const input = screen.getByLabelText('设备名称')
    fireEvent.change(input, { target: { value: '尚未提交' } })
    fireEvent.compositionStart(input)
    fireEvent.keyDown(input, { key: 'Escape', isComposing: true })
    expect(screen.getByLabelText('设备名称')).toHaveValue('尚未提交')
    fireEvent.compositionEnd(input)
    await click('退出登录'); await click('我的')
    expect(screen.queryByLabelText('设备名称')).not.toBeInTheDocument()
    expect(screen.getByText('研究用 Mac')).toBeInTheDocument()
  })

  it('连接更换Escape还原地址；手动API留空可发现且自动切换保留草稿', async () => {
    const { state, calls } = await mount()
    await click('连接')
    fireEvent.change(screen.getByLabelText('服务地址'), { target: { value: 'https://next.example.com' } })
    fireEvent.keyDown(screen.getByLabelText('服务地址'), { key: 'Escape' })
    expect(screen.getByLabelText('服务地址')).toHaveValue(state.service)
    await click('手动填写')
    fireEvent.change(screen.getByLabelText('API 地址'), { target: { value: 'https://api.example.com' } })
    await click('恢复自动识别'); await click('手动填写')
    expect(screen.getByLabelText('API 地址')).toHaveValue('https://api.example.com')
    fireEvent.change(screen.getByLabelText('API 地址'), { target: { value: '' } })
    fireEvent.change(screen.getByLabelText('服务地址'), { target: { value: 'https://next.example.com' } })
    await click('连接并授权')
    expect(calls).toHaveBeenCalledWith({ type: 'connect', service: 'https://next.example.com', api: '' })
  })

  it('新一轮授权不显示前次浏览器失败，授权后连接显示真实阶段说明', async () => {
    const { state } = await mount({ connected: false, connection: 'authorizing', authorizationPhase: 'waiting' }, async action => action.type === 'open-authorization' ? { ok: false, field: '', message: '浏览器未能打开' } : { ok: true })
    await click('重新打开浏览器')
    expect(screen.getByRole('alert')).toHaveTextContent('浏览器未能打开')
    act(() => publish({ ...state, connection: 'unpaired', authorizationPhase: 'idle' }))
    await click('连接并授权')
    act(() => publish(state))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    act(() => publish({ ...state, connection: 'connecting', connectionReason: 'authorization', authorizationPhase: 'idle' }))
    expect(screen.getByText('授权已完成，正在连接服务')).toBeInTheDocument()
  })
})

describe('更新阶段与恢复', () => {
  const cases: [UpdatePhase, string, boolean][] = [
    ['checking', '正在检查更新', false], ['available', '检测到新版本 0.1.2', false], ['latest', '已是最新版本', false], ['check-error', '检查更新失败', false],
    ['downloading', '正在下载更新', false], ['download-error', '下载失败', false], ['verifying', '正在校验安装包', false], ['verify-error', '校验失败', false],
    ['waiting', '等待本机操作完成', false], ['installing', '正在安装更新', true], ['install-error', '安装失败', false], ['restarting', '正在重新启动', true],
    ['restart-error', '未能重新启动', false], ['success', '已更新至 0.1.1', false],
  ]
  it.each(cases)('%s 展示对应阶段并遵守取消边界', async (phase, title, locked) => {
    await mount({ update: { phase, percent: 47, currentVersion: '0.1.1', targetVersion: '0.1.2', failure: 'none' } })
    const dialog = screen.getByRole('dialog', { name: title })
    expect(dialog).toBeInTheDocument()
    if (locked) expect(within(dialog).queryByRole('button')).not.toBeInTheDocument()
    else expect(within(dialog).getByRole('button', { name: /取消|完成|关闭|稍后/ })).toBeEnabled()
    if (phase === 'downloading' || phase === 'installing') expect(within(dialog).getByRole('progressbar')).toHaveAttribute('aria-valuenow', '47')
    else if (within(dialog).queryByRole('progressbar')) expect(within(dialog).getByRole('progressbar')).not.toHaveAttribute('aria-valuenow')
  })

  it('旧快照不能覆盖当前状态，安装阶段Escape不会请求取消', async () => {
    const { state, calls } = await mount({ update: { phase: 'installing', percent: 47, currentVersion: '0.1.1', targetVersion: '0.1.2', failure: 'none' } })
    const dialog = screen.getByRole('dialog')
    fireEvent.keyDown(dialog, { key: 'Escape' })
    act(() => window.receiveDesktopState({ ...state, revision: 0, update: { ...state.update, phase: 'available' } }))
    expect(screen.getByRole('dialog', { name: '正在安装更新' })).toBeInTheDocument()
    expect(calls.mock.calls.some(([action]) => action.type === 'update-cancel')).toBe(false)
  })
})
