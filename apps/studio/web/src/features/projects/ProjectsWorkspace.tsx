import type { EditorState } from '@codemirror/state'
import { FolderPlus, Plus } from 'lucide-react'
import { useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { startNotificationFeed } from '../../api/notifications'
import type { AuthUser } from '../../api/auth/types'
import { BrandLogo } from '../../components/ui/BrandLogo'
import { Button, Dialog, FeedbackState, TextField, ValidatedForm } from '../../components/ui'
import { useI18n } from '../../i18n'
import type { DraftAttachment } from '../conversation/useAttachments'
import type { Project } from './api'
import { useProjectManagement } from './useProjectManagement'
import './projects.css'

export interface ProjectWorkspaceScope {
  project: Project
  projects: Project[]
  select: (id: string, threadId?: string) => void
  create: () => void
  rename: () => void
  renameForm: {
    name: string
    error: string
    validationAttempt: number
    saving: boolean
    change: (value: string) => void
    submit: () => Promise<void>
    cancel: () => void
  } | null
  modalOpen: boolean
  drafts: Map<string, EditorState>
  attachments: Map<string, DraftAttachment[]>
}

/** 项目变化使旧页面卸载；未发送的输入按项目和会话留在当前登录工作区 */
export function ProjectsWorkspace({ user, onLogout, children }: {
  user: AuthUser; onLogout: () => void; children: (scope: ProjectWorkspaceScope) => ReactNode
}) {
  const { t } = useI18n()
  const input = useRef<HTMLInputElement>(null)
  const [drafts] = useState(() => new Map<string, EditorState>())
  const [attachments] = useState(() => new Map<string, DraftAttachment[]>())
  const { project, projects, status, editor, name, error, validationAttempt, saving, select, openEditor, submit, setName, closeEditor, retry } = useProjectManagement(user)
  const projectId = project?.id
  useLayoutEffect(() => startNotificationFeed(projectId), [projectId])
  return <>
    {project ? children({ project, projects, select, create: () => openEditor(null), rename: () => openEditor(project),
      renameForm: editor?.project?.id === project.id ? { name, error, validationAttempt, saving, change: setName, submit, cancel: closeEditor } : null,
      modalOpen: editor !== null && editor.project === null, drafts, attachments }) : (
      <main className="projects-onboarding" inert={editor !== null || undefined} aria-hidden={editor !== null || undefined}>
        <div className="projects-onboarding-brand"><BrandLogo size="md" /></div>
        <section>
          {status === 'loading' ? <FeedbackState kind="loading" title={t('正在加载项目')} /> : status === 'error' ? <FeedbackState kind="error" title={t('项目加载失败')} onRetry={retry} /> : <>
            <FolderPlus size={32} aria-hidden="true" />
            <h1>{t('从一个项目开始')}</h1>
            <p>{t('让会话、技能与记忆各有归属')}</p>
            <Button type="button" variant="primary" leadingIcon={<Plus size={18} />} onClick={() => openEditor(null)}>{t('创建项目')}</Button>
          </>}
        </section>
        <Button type="button" variant="text" onClick={onLogout}>{t('退出登录')}</Button>
      </main>
    )}
    <Dialog open={editor !== null && editor.project === null} title={t('创建项目')} closeDisabled={saving} initialFocusRef={input} restoreFocusTo={editor?.trigger} onClose={closeEditor}>
      <ValidatedForm className="projects-form" errors={{ projectName: error }} validationAttempt={validationAttempt} onSubmit={event => { event.preventDefault(); void submit() }}>
        <TextField ref={input} name="projectName" label={t('项目名称')} value={name} maxLength={64} required onChange={event => setName(event.target.value)} error={error} disabled={saving} autoComplete="off" />
        <div className="projects-form-actions"><Button type="button" variant="secondary" disabled={saving} onClick={closeEditor}>{t('取消')}</Button><Button type="submit" variant="primary" loading={saving}>{t('确认')}</Button></div>
      </ValidatedForm>
    </Dialog>
  </>
}
