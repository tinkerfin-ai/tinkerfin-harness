import { ModalDialog } from './ModalDialog'
import { useI18n } from '../../../i18n'

export type WorkspaceDialog =
  | { kind: 'rename'; threadId: string; initialValue: string; restoreFocusTo?: HTMLElement | null }
  | { kind: 'delete'; threadId: string; title: string; isRunning: boolean; restoreFocusTo?: HTMLElement | null }
  | { kind: 'disable-plan'; threadId: string }

export function WorkspaceDialogs({
  dialog,
  pending,
  onConfirm,
  onCancel,
}: {
  dialog: WorkspaceDialog | null
  pending: boolean
  onConfirm: (value?: string) => void | Promise<void>
  onCancel: () => void
}) {
  const { t } = useI18n()
  if (!dialog) return null

  if (dialog.kind === 'rename') {
    return (
      <ModalDialog
        open
        title={t('重命名会话')}
        inputLabel={t('会话名称')}
        initialValue={dialog.initialValue}
        confirmLabel={t('保存')}
        isPending={pending}
        restoreFocusTo={dialog.restoreFocusTo}
        onConfirm={onConfirm}
        onCancel={onCancel}
      />
    )
  }

  if (dialog.kind === 'delete') {
    return (
      <ModalDialog
        open
        title={t('删除会话')}
        description={dialog.isRunning
          ? t('将先停止“{title}”的当前运行，再删除会话。自动化执行历史和结果会保留', { title: dialog.title })
          : t('将删除“{title}”。普通会话的历史会永久删除；自动化执行历史和结果会保留', { title: dialog.title })}
        confirmLabel={t('删除')}
        tone="danger"
        isPending={pending}
        restoreFocusTo={dialog.restoreFocusTo}
        onConfirm={onConfirm}
        onCancel={onCancel}
      />
    )
  }

  if (dialog.kind === 'disable-plan') {
    return (
      <ModalDialog
        open
        title={t('关闭当前 Plan？')}
        description={t('当前 Plan 澄清或审阅将被取消，不会执行旧计划。Tool/Filesystem 审批不受影响')}
        confirmLabel={t('关闭 Plan')}
        isPending={pending}
        onConfirm={onConfirm}
        onCancel={onCancel}
      />
    )
  }

  return null
}
