import type { DesktopState } from '../src/state'

/** 只用于受控桥接测试，合成设备与发行版本不会进入应用包 */
export function fixture(patch: Partial<DesktopState> = {}): DesktopState {
  return {
    revision: 1, surface: 'workbench', language: 'zh-Hans', locale: 'zh-Hans', theme: 'light', ready: true,
    connected: true, connection: 'online', connectionReason: 'restore', authorizationPhase: 'idle', authorizationError: '', connectionError: '',
    service: 'https://studio.example.com', api: 'https://studio.example.com', switching: false,
    name: '研究用 Mac', account: 'yunsan', version: '0.1.1', busy: '',
    projects: [{ id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', name: '量化研究' }, { id: 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', name: '市场报告' }],
    projectsLoading: false, openingProject: '', permissions: { accessibility: false, screenCapture: true },
    update: { phase: 'idle', percent: 0, currentVersion: '0.1.1', targetVersion: '0.1.2' }, resume: null,
    ...patch,
  }
}
