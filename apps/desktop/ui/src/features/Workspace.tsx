import { ArrowUpRight, ChevronDown, Folder, LoaderCircle, RotateCw } from 'lucide-react'
import { useLayoutEffect, useRef, useState } from 'react'
import { Button } from '../components/ui/Button'
import { IconButton } from '../components/ui/IconButton'
import { send } from '../bridge'
import { useI18n } from '../i18n'
import type { DesktopState } from '../state'
import { useAction } from './useAction'

export function Workspace({ state }: { state: DesktopState }) {
  const { t } = useI18n()
  const refresh = useAction()
  const open = useAction()
  const viewport = useRef<HTMLDivElement>(null)
  const content = useRef<HTMLElement>(null)
  const moreButton = useRef<HTMLButtonElement>(null)
  const [more, setMore] = useState(false)
  useLayoutEffect(() => {
    const scroll = viewport.current, list = content.current
    if (!scroll || !list) return
    const measure = () => {
      const available = scroll.clientHeight > 0 && scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight > 1
      if (!available && scroll.clientHeight > 0 && document.activeElement === moreButton.current) scroll.focus({ preventScroll: true })
      setMore(available)
    }
    const observer = new ResizeObserver(measure)
    observer.observe(scroll); observer.observe(list)
    scroll.addEventListener('scroll', measure, { passive: true })
    measure()
    return () => { observer.disconnect(); scroll.removeEventListener('scroll', measure) }
  }, [])
  return <>
    <div className="pane-toolbar"><IconButton label={t('刷新')} tooltip={t('刷新')} icon={<RotateCw size={16} />} variant="ghost" loading={state.projectsLoading || refresh.pending} disabled={open.pending || Boolean(state.openingProject)} onClick={() => void refresh.run({ type: 'refresh-projects' })} /></div>
    <main className="workspace-content" aria-label={t('工作区')}>
    <div ref={viewport} id="workspace-projects" className="client-body" role="region" tabIndex={0} aria-label={t('项目目录')}>
      <section ref={content} className="projects" aria-busy={state.projectsLoading}>
        {state.projectsLoading && state.projects.length === 0 ? [0, 1, 2].map(index => <div className="skeleton-row" key={index} aria-hidden="true"><i /><span /></div>)
          : state.projects.length === 0 ? <div className="section-empty"><p>{t('暂无项目')}</p></div>
          : <div className="project-list">
            {state.projects.map(project => <button type="button" className="project-item" key={project.id} disabled={open.pending || Boolean(state.openingProject)} aria-busy={state.openingProject === project.id || undefined}
              aria-label={`${project.name} · ${t('在 Finder 中打开')}`} title={project.name} onClick={() => void open.run({ type: 'open-project', id: project.id })}>
              <Folder aria-hidden="true" /><span className="project-name">{project.name}</span><span className="row-action-icon">{state.openingProject === project.id ? <LoaderCircle className="spinner" aria-hidden="true" /> : <ArrowUpRight aria-hidden="true" />}</span>
            </button>)}
          </div>}
      </section>
    </div>
    {more && <div className="workspace-scroll-hint"><IconButton ref={moreButton} label={t('向下查看更多项目')} aria-controls="workspace-projects" icon={<ChevronDown />} variant="ghost" onClick={() => {
      const scroll = viewport.current
      if (scroll) { scroll.focus({ preventScroll: true }); scroll.scrollBy({ top: scroll.clientHeight * .8 }) }
    }} /></div>}
    </main>
    <footer className="pane-actions"><Button variant="primary" leadingIcon={<ArrowUpRight />} onClick={() => void send({ type: 'open-studio' })}>{t('打开 Studio')}</Button></footer>
  </>
}
