import { FolderKanban } from 'lucide-react'
import type { RefObject } from 'react'
import { Button, TextField, ValidatedForm } from '../../components/ui'
import { useI18n } from '../../i18n'

export interface ProjectEditorForm {
  projectId: string | null
  name: string
  error: string
  validationAttempt: number
  saving: boolean
  change: (value: string) => void
  submit: () => Promise<boolean>
  cancel: () => void
}

export function ProjectNameForm({ form, inputRef, onSubmit, onCancel }: {
  form: ProjectEditorForm
  inputRef: RefObject<HTMLInputElement | null>
  onSubmit: () => void
  onCancel: () => void
}) {
  const { t } = useI18n()
  return <ValidatedForm className="projects-form" aria-label={t(form.projectId ? '重命名项目' : '创建项目')} errors={{ projectName: form.error }} validationAttempt={form.validationAttempt}
    onSubmit={event => { event.preventDefault(); onSubmit() }}>
    <TextField ref={inputRef} name="projectName" label={t('项目名称')} leadingContent={<FolderKanban size={16} />} value={form.name} maxLength={64} required
      onChange={event => form.change(event.target.value)} error={form.error} disabled={form.saving} autoComplete="off" shape="standard" fieldSize="md" />
    <div className="projects-form-actions"><Button type="button" variant="ghost" disabled={form.saving} onClick={onCancel}>{t('取消')}</Button>
      <Button type="submit" variant="primary" loading={form.saving} disabled={!form.name.trim()}>{t(form.projectId ? '保存' : '创建')}</Button></div>
  </ValidatedForm>
}
