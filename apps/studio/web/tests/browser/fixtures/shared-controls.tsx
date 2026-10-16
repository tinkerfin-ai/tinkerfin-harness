import { useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { FeedbackState } from '../../../src/components/ui/FeedbackState'
import { BrandLogo } from '../../../src/components/ui/BrandLogo'
import { BrandMark } from '../../../src/components/ui/BrandMark'
import { Button } from '../../../src/components/ui/Button'
import { OverlayScrollbar } from '../../../src/components/ui/OverlayScrollbar'
import { TextField } from '../../../src/components/ui/TextField'
import { UserAvatar } from '../../../src/components/ui/UserAvatar'
import { ViewTabs } from '../../../src/components/ui/ViewTabs'
import '../../../src/styles/tokens.css'
import '../../../src/styles/typography.css'
import '../../../src/styles/fonts.css'
import '../../../src/styles/global.css'
import '../../../src/components/ui/ui.css'
import '../../../src/features/conversation/chainTrace/chainTrace.css'
import '../../../src/features/workspaceFiles/workspaceFiles.css'
import '../../../src/features/workspace/components/conversation-search.css'

const parameters = new URLSearchParams(location.search)
document.documentElement.dataset.theme = parameters.get('theme') ?? 'light'

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

createRoot(document.getElementById('root')!).render(parameters.get('example') === 'scrollbar' ? <ScrollbarExample /> : parameters.get('example') === 'loading' ? <LoadingExamples /> : <Controls />)
