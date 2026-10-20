import type { EditorState } from '@codemirror/state'
import { FolderPlus, Plus } from 'lucide-react'
import { useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from 'react'
import { startNotificationFeed } from '../../api/notifications'
import type { ToastHandler } from '../../components/ui/ToastViewport'
import { useAutomationRuns, type AutomationRuns } from '../automation/useAutomationRuns'
import type { AuthUser } from '../../api/auth/types'
import { BrandLogo } from '../../components/ui/BrandLogo'
import { Button, Dialog, FeedbackState } from '../../components/ui'
import { useI18n } from '../../i18n'
import { useRunConversation, type OpenRunConversation } from '../automation/useRunConversation'
import type { DraftAttachment } from '../conversation/useAttachments'
import type { Project } from './api'
import { useProjectManagement } from './useProjectManagement'
import { ProjectNameForm, type ProjectEditorForm } from './ProjectNameForm'
import './projects.css'

export interface ProjectWorkspaceScope {
  automation: AutomationRuns
  openRunConversation: OpenRunConversation
  project: Project
  projects: Project[]
  select: (id: string, threadId?: string) => void
  create: (name?: string) => void
  rename: (projectId: string) => void
  editorForm: ProjectEditorForm | null
  pickerFocusPending: RefObject<boolean>
  pickerTriggerRef: RefObject<HTMLButtonElement | null>
  pickerOpen: boolean
  openPicker: () => void
  closePicker: () => void
  drafts: Map<string, EditorState>
  attachments: Map<string, DraftAttachment[]>
}

/** 项目变化使旧页面卸载；未发送的输入按项目和会话留在当前登录工作区 */
export function ProjectsWorkspace({ user, onLogout, onToast, children }: {
  user: AuthUser; onLogout: () => void; onToast?: ToastHandler; children: (scope: ProjectWorkspaceScope) => ReactNode
}) {
  const { t } = useI18n()
  const [pickerOpen, setPickerOpen] = useState(false)
  const pickerTriggerRef = useRef<HTMLButtonElement>(null)
  const pickerFocusPending = useRef(false)
  const input = useRef<HTMLInputElement>(null)
  const [drafts] = useState(() => new Map<string, EditorState>())
  const [attachments] = useState(() => new Map<string, DraftAttachment[]>())
  const { project, projects, status, editor, name, error, validationAttempt, saving, select, openEditor, submit, setName, closeEditor, retry } = useProjectManagement(user)
  const editorForm: ProjectEditorForm | null = editor ? { projectId: editor.project?.id ?? null, name, error, validationAttempt, saving, change: setName, submit, cancel: closeEditor } : null
  const projectKey = projects.map(item => item.id).sort().join(',')
  useLayoutEffect(() => startNotificationFeed(), [projectKey])
  const openRunConversation = useRunConversation(target => {
    select(target.projectId, target.threadId)
    window.dispatchEvent(new PopStateEvent('popstate'))
  }, onToast)
  const automation = useAutomationRuns(user.user_id, projects.map(item => item.id), onToast)
  return <>
    {project ? children({ automation, openRunConversation, project, projects, select, create: name => openEditor(null, name), rename: id => { const target = projects.find(item => item.id === id); if (target) openEditor(target) },
      editorForm, pickerFocusPending, pickerTriggerRef, pickerOpen, openPicker: () => setPickerOpen(true), closePicker: () => { pickerFocusPending.current = true; setPickerOpen(false) },
      drafts, attachments }) : (
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
    <Dialog open={!project && editorForm !== null} title={t('创建项目')} closeDisabled={saving} initialFocusRef={input} restoreFocusTo={editor?.trigger} onClose={closeEditor}>
      {editorForm && <ProjectNameForm form={editorForm} inputRef={input} onSubmit={() => { void submit() }} onCancel={closeEditor} />}
    </Dialog>
  </>
}
