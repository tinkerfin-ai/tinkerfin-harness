import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/shared/http'
import { AutomationEditor } from './AutomationEditor'
import { taskFixture } from '../../test/automationFixtures'
import { saveTask } from './api'

vi.mock('./api', () => ({ saveTask: vi.fn() }))
const choices = { defaultModelId: 'main', renderModelChoice: () => <span>主模型</span> }
beforeEach(() => vi.mocked(saveTask).mockReset())

describe('自动化编辑器', () => {
  it('校验后提交真实配置，确认保存前不关闭', async () => {
    const saved = vi.fn()
    vi.mocked(saveTask).mockResolvedValue(taskFixture())
    render(<AutomationEditor {...choices} trigger={null} onSave={saved} onClose={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: '创建任务' }))
    expect(saveTask).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText('任务名称'), { target: { value: '晨间简报' } })
    fireEvent.change(screen.getByLabelText('任务指令'), { target: { value: '整理新闻' } })
    fireEvent.click(screen.getByRole('button', { name: '创建任务' }))
    await waitFor(() => expect(saved).toHaveBeenCalledOnce())
    expect(saveTask).toHaveBeenCalledWith(expect.objectContaining({ name: '晨间简报', modelId: 'main', accessMode: 'full' }), expect.any(String), undefined, expect.any(AbortSignal))
  })
  it('失败保留输入，原样重试复用请求ID', async () => {
    vi.mocked(saveTask).mockRejectedValueOnce(new ApiError('任务已变化', { status: 409 })).mockResolvedValue(taskFixture())
    render(<AutomationEditor {...choices} trigger={null} task={taskFixture()} onSave={vi.fn()} onClose={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('任务名称'), { target: { value: '我的输入' } })
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('任务已变化')
    expect(screen.getByLabelText('任务名称')).toHaveValue('我的输入')
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }))
    await waitFor(() => expect(saveTask).toHaveBeenCalledTimes(2))
    expect(vi.mocked(saveTask).mock.calls[0][1]).toBe(vi.mocked(saveTask).mock.calls[1][1])
  })
  it('空执行日和非法间隔不能保存', () => {
    render(<AutomationEditor {...choices} trigger={null} task={taskFixture({ schedule: { kind: 'weekly', weekdays: [4], time: '17:00' } })} onSave={vi.fn()} onClose={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: /执行频率/ }))
    fireEvent.click(screen.getByRole('button', { name: '周五' }))
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }))
    expect(screen.getByText('请至少选择一个执行日')).toBeInTheDocument()
    expect(saveTask).not.toHaveBeenCalled()
  })
})
