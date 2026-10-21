import '../../../src/styles/tokens.css'
import { Dialog, DatePicker, TimePicker, ExpandableSearch, ListboxPicker } from '../../../src/components/ui'
import { useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { ArchiveRestore, Workflow } from 'lucide-react'
import { IconButton } from '../../../src/components/ui/IconButton'
import { ComputerResources } from '../../../src/features/devices/ComputerResources'
import type { AccessMode } from '../../../src/types'
import { LocaleProvider } from '../../../src/i18n'
import { FeedbackState } from '../../../src/components/ui/FeedbackState'
import { BrandLogo } from '../../../src/components/ui/BrandLogo'
import { BrandMark } from '../../../src/components/ui/BrandMark'
import { Button } from '../../../src/components/ui/Button'
import { OverlayScrollbar } from '../../../src/components/ui/OverlayScrollbar'
import { TextField } from '../../../src/components/ui/TextField'
import { UserAvatar } from '../../../src/components/ui/UserAvatar'
import { ViewTabs } from '../../../src/components/ui/ViewTabs'
import '../../../src/styles/typography.css'
import '../../../src/styles/fonts.css'
import '../../../src/styles/global.css'
import '../../../src/components/ui/ui.css'
import '../../../src/features/conversation/chainTrace/chainTrace.css'
import '../../../src/features/workspaceFiles/workspaceFiles.css'
import '../../../src/features/workspace/components/conversation-search.css'
import '../../../src/features/workspace/workspace.css'
import { ConversationRunIndicator } from '../../../src/features/workspace/components/ConversationRunIndicator'

const parameters = new URLSearchParams(location.search)
document.documentElement.dataset.theme = parameters.get('theme') ?? 'light'

function FocusReturnExamples() {
  const [dialogOpen, setDialogOpen] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const [pickerOpen, setPickerOpen] = useState(false)
  const [query, setQuery] = useState('')
  const input = useRef<HTMLInputElement>(null)
  return <main style={{ padding: 24, display: 'flex', gap: 16, alignItems: 'center' }}>
    <Button type="button" onClick={() => setDialogOpen(true)}>打开弹窗</Button>
    <Dialog open={dialogOpen} title="焦点验证" initialFocusRef={input} onClose={() => setDialogOpen(false)}><TextField ref={input} label="弹窗输入" /></Dialog>
    <ExpandableSearch label="展开搜索" closeLabel="关闭搜索" placeholder="搜索" open={expanded} onOpenChange={setExpanded} value={query} onChange={setQuery} />
    <ListboxPicker value="甲" options={['甲', '乙']} open={pickerOpen} onOpenChange={setPickerOpen} onChange={() => {}}
      triggerLabel="打开选项" listboxLabel="选项" rootClassName="ui-compact-picker" triggerClassName="ui-compact-picker-trigger" listboxClassName="ui-compact-picker-options"
      renderTrigger={value => value} renderOption={value => value} />
    <TimePicker label="选择时间" value="09:00" onChange={() => {}} />
    <DatePicker label="选择日期" value="2026-10-20" onChange={() => {}} />
    <Button type="button">后续操作</Button>
  </main>
}

function ConversationIndicatorExamples() {
  const [second, setSecond] = useState(false)
  const [generation, setGeneration] = useState(0)
  return <main>
    <section aria-label="会话一"><ConversationRunIndicator /></section>
    {second && <section aria-label="会话二"><ConversationRunIndicator key={generation} /></section>}
    <button type="button" onClick={() => setSecond(value => !value)}>切换第二个会话</button>
    <button type="button" onClick={() => setGeneration(value => value + 1)}>重建第二个会话</button>
  </main>
}

function IconButtonExamples() {
  const [accessMode, setAccessMode] = useState<AccessMode>('write_approval')
  const deviceState = parameters.get('deviceState') ?? 'online'
  const [settingsOpened, setSettingsOpened] = useState(false)
  const [trace, setTrace] = useState(false)
  const [archived, setArchived] = useState(false)
  return <main style={{ padding: 24, display: 'flex', alignItems: 'center', gap: 16, flexWrap: 'wrap' }}>
    <ComputerResources value={accessMode} onChange={setAccessMode} onOpenSettings={() => setSettingsOpened(true)} disabled={parameters.get('locked') === 'true'}
      devices={{ status: deviceState === 'loading' || deviceState === 'error' ? deviceState : 'ready', refresh: () => {}, devices: deviceState === 'empty' || deviceState === 'loading' || deviceState === 'error' ? [] : [{ device_id: 'review', name: parameters.get('deviceName') ?? 'Review Mac', online: deviceState === 'online', revoked: false,
        paired_at: '2030-01-01T00:00:00Z', last_seen_at: null, capabilities: { browser: false, desktop: false, mcp: false, desktop_capture: false, open_url: false } }] }} />
    {settingsOpened && <output>设备设置已打开</output>}
    <IconButton label="链路" icon={<Workflow size={17} />} selected={trace} onClick={() => setTrace(value => !value)} />
    <IconButton label="已归档会话" icon={<ArchiveRestore size={17} />} selected={archived} onClick={() => setArchived(value => !value)} />
    <IconButton label="不可用" icon={<Workflow size={17} />} selected disabled />
  </main>
}

function ScrollbarExample() {
  const viewport = useRef<HTMLDivElement>(null)
  const [mounted, setMounted] = useState(true)
  const [overflow, setOverflow] = useState(true)
  const horizontal = parameters.get('axis') === 'horizontal'
  return <>
    <button type="button" onClick={() => setOverflow(value => !value)}>切换内容高度</button>
    <button type="button" onClick={() => setMounted(false)}>移除滚动区域</button>
    {mounted && <section aria-label="滚动容器" style={{ position: 'relative', width: 280 }}>
      <div ref={viewport} className="ui-scrollbar" role="region" aria-label="可滚动内容" tabIndex={0}
        style={{ width: 280, height: 200, overflow: 'auto' }}>
        <div style={{ width: horizontal && overflow ? 1120 : 280, height: !horizontal && overflow ? 800 : 200 }}>
          <button type="button">查看详情</button>
          <p>内容保留原生滚动和键盘焦点</p>
        </div>
      </div>
      <OverlayScrollbar viewportRef={viewport} axis={horizontal ? 'horizontal' : 'vertical'}
        size={parameters.get('size') === 'compact' ? 'compact' : 'regular'}
        visibility={parameters.get('visibility') === 'persistent' ? 'persistent' : 'transient'} />
    </section>}
    <button type="button">后续操作</button>
  </>
}


function LoadingExamples() {
  const contexts = [
    ['页面', ''], ['紧凑区域', ''], ['链路', 'chain-trace'], ['链路详情', 'chain-trace-details'],
    ['搜索', 'conversation-search-results'], ['工作区', 'workspace-files-feedback'],
  ]
  return <main style={{ padding: 16, display: 'grid', gap: 16 }}>
    <h1>加载反馈验证</h1>
    {contexts.map(([name, className]) => <section key={name} aria-label={name} style={{ minWidth: 0 }}>
      <h2>{name}</h2>
      <div className={className}><FeedbackState kind="loading" compact={name === '紧凑区域'} title="正在加载历史会话" /></div>
    </section>)}
    <section aria-label="长加载文案" style={{ minWidth: 0 }}>
      <FeedbackState kind="loading" title="正在读取这份包含较长名称的工作区文件，请稍候 Loading conversation history and workspace details" />
    </section>
    <section aria-label="错误恢复"><FeedbackState kind="error" appearance="retry" title="历史会话加载失败" retryLabel="重新加载" onRetry={() => {}} /></section>
  </main>
}

export function Controls() {
  return <main style={{ padding: 16, display: 'grid', gap: 24 }}>
    <h1>共享控件验证</h1>
    {(['md', 'lg'] as const).flatMap(size => (['round', 'capsule', 'standard'] as const).map(shape =>
      <TextField key={`${size}-${shape}`} label={`输入 ${size} ${shape}`} fieldSize={size} shape={shape} />))}
    {(['regular', 'medium', 'compact'] as const).map(density => <ViewTabs key={density} label={`页签 ${density}`}
      density={density} value="first" onChange={() => {}} options={[{ value: 'first', label: '首项' }, { value: 'second', label: '次项' }]} />)}
    {(['sm', 'md', 'lg'] as const).map(size => <BrandLogo key={size} size={size} className={`fixture-brand-${size}`} />)}
    <BrandMark size={28} className="fixture-mark" />
    {(['sm', 'lg'] as const).map(size => <UserAvatar key={size} size={size} avatarUrl={null} className={`fixture-avatar-${size}`} />)}
    {(['primary', 'secondary', 'solid'] as const).map(variant => <Button key={variant} variant={variant} size="sm" shape="capsule">操作 {variant}</Button>)}
  </main>
}

createRoot(document.getElementById('root')!).render(parameters.get('example') === 'focus-return' ? <FocusReturnExamples /> : parameters.get('example') === 'conversation-indicators' ? <ConversationIndicatorExamples /> : parameters.get('example') === 'icons' ? <LocaleProvider><IconButtonExamples /></LocaleProvider> : parameters.get('example') === 'scrollbar' ? <ScrollbarExample /> : parameters.get('example') === 'loading' ? <LoadingExamples /> : <Controls />)
