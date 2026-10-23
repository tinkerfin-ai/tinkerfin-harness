import { createContext, useContext } from 'react'

const english: Record<string, string> = {
  '请检查本机配置后重试': 'Check local settings and try again',
  '连接': 'Connection', '工作区': 'Workspace', '权限': 'Permissions', '我的': 'My account',
  '工作台导航': 'Workspace navigation', '服务地址': 'Service address', 'API 地址': 'API address',
  '自动识别': 'Automatic', '手动填写': 'Enter manually', '恢复自动识别': 'Use automatic discovery',
  '连接并授权': 'Connect & authorize', '打开 Studio': 'Open Studio', '取消': 'Cancel',
  '请输入服务地址': 'Enter a service address', '请输入完整的 http 或 https 地址': 'Enter a full http or https address',
  '请检查地址后重试': 'Check the address and try again', '无法识别 API 地址，请填写': 'Enter the API address to continue',
  '新授权成功后将更换连接，本机执行会中断': 'Once authorized, the new connection replaces this one and interrupts local execution',
  '请在浏览器完成授权': 'Complete authorization in your browser', '取消授权': 'Cancel authorization',
  '重新打开浏览器': 'Reopen browser', '正在完成连接': 'Finishing connection', '授权已完成，正在连接服务': 'Authorized. Connecting to the service',
  '正在启动': 'Starting', '连接失败': 'Connection failed', '连接已中断': 'Connection interrupted',
  '请检查网络后重新连接': 'Check your network and reconnect', '请检查服务地址后重试': 'Check the service address and try again',
  '重新连接': 'Reconnect', '重新授权': 'Authorize again', '授权已失效，请重新连接': 'Authorization expired. Connect again',
  '项目目录': 'Project folders', '暂无项目': 'No projects', '刷新': 'Refresh', '在 Finder 中打开': 'Open in Finder', '向下查看更多项目': 'Scroll to more projects',
  '辅助功能': 'Accessibility', '屏幕录制': 'Screen recording', '读取控件、点击与输入': 'Read controls, click and type',
  '截取应用窗口': 'Capture app windows', '已开启': 'Enabled', '前往开启': 'Enable in Settings',
  '账号': 'Account', '设备名称': 'Device name', '编辑设备名称': 'Edit device name', '保存设备名称': 'Save device name',
  '请输入设备名称': 'Enter a device name', '保存失败，请重试': 'Couldn’t save. Try again',
  '语言': 'Language', '跟随系统': 'System', '当前版本': 'Current version', '检查更新': 'Check for updates', '退出登录': 'Sign out',
  '已连接': 'Connected', '连接中': 'Connecting', '未连接': 'Disconnected', '设置': 'Settings', '退出': 'Quit',
  '正在检查更新': 'Checking for updates', '检测到新版本 {version}': 'Version {version} is available',
  '已是最新版本': 'You’re up to date', '检查更新失败': 'Couldn’t check for updates', '正在下载更新': 'Downloading update',
  '下载失败': 'Download failed', '正在校验安装包': 'Verifying update', '校验失败': 'Verification failed',
  '等待本机操作完成': 'Waiting for local operations', '正在安装更新': 'Installing update', '安装失败': 'Installation failed',
  '正在重新启动': 'Restarting', '未能重新启动': 'Couldn’t restart', '已更新至 {version}': 'Updated to {version}',
  '是否更新？更新后将重新启动': 'Update now? The app will restart', '当前版本 {version}': 'Current version {version}',
  '请检查网络后重试': 'Check your network and try again', '请重新下载安装包': 'Download the update again',
  '请检查磁盘空间和目录权限': 'Check disk space and folder permissions', '更新文件不可用，请稍后重试': 'Update files unavailable. Try again later',
  '当前版本仍可使用': 'Your current version is still available', '下载进度': 'Download progress', '安装进度': 'Installation progress',
  '完成': 'Done', '关闭': 'Close', '稍后': 'Later', '立即更新': 'Update now', '重试': 'Try again',
  '重新下载': 'Download again', '重新启动': 'Restart', '关闭对话框': 'Close dialog', '加载中': 'Loading',
  '界面暂不可用': 'Interface unavailable', '重新打开': 'Reopen', '请重试': 'Try again',
}

export function translate(locale: string, key: string) { return locale === 'en' ? (english[key] ?? key) : key }
export const LocaleContext = createContext({ t: (key: string) => key })
export function useI18n() { return useContext(LocaleContext) }
