import type { Page } from '@playwright/test'

/** 为单项目场景提供当前项目目录，具体项目管理用例自行定义响应 */
export async function installProjectScope(page: Page): Promise<void> {
  await page.route('**/api/projects', route => route.fulfill({ json: {
    code: 0, message: 'success', data: [{ id: 'project-1', name: '测试项目', createdAt: '2030-01-01', updatedAt: '2030-01-01' }],
  } }))
}
