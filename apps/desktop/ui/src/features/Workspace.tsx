import { ArrowUpRight, Folder, LoaderCircle, RotateCw } from 'lucide-react'
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
  return <>
    <div className="pane-toolbar"><IconButton label={t('刷新')} tooltip={t('刷新')} icon={<RotateCw size={16} />} variant="ghost" loading={state.projectsLoading || refresh.pending} disabled={open.pending || Boolean(state.openingProject)} onClick={() => void refresh.run({ type: 'refresh-projects' })} /></div>
    <main className="client-body" aria-label={t('工作区')}>
      <section className="projects" aria-label={t('项目目录')} aria-busy={state.projectsLoading}>
        {state.projectsLoading && state.projects.length === 0 ? [0, 1, 2].map(index => <div className="skeleton-row" key={index} aria-hidden="true"><i /><span /></div>)
          : state.projects.length === 0 ? <div className="section-empty"><p>{t('暂无项目')}</p></div>
          : <div className="project-list" role="region" tabIndex={0} aria-label={t('项目目录')}>
            {state.projects.map(project => <button type="button" className="project-item" key={project.id} disabled={open.pending || Boolean(state.openingProject)} aria-busy={state.openingProject === project.id || undefined}
              aria-label={`${project.name} · ${t('在 Finder 中打开')}`} title={project.name} onClick={() => void open.run({ type: 'open-project', id: project.id })}>
              <Folder aria-hidden="true" /><span className="project-name">{project.name}</span><span className="row-action-icon">{state.openingProject === project.id ? <LoaderCircle className="spinner" aria-hidden="true" /> : <ArrowUpRight aria-hidden="true" />}</span>
            </button>)}
          </div>}
      </section>
    </main>
    <footer className="pane-actions"><Button variant="primary" leadingIcon={<ArrowUpRight />} onClick={() => void send({ type: 'open-studio' })}>{t('打开 Studio')}</Button></footer>
  </>
}
