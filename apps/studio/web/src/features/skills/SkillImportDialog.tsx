import { Check, FileArchive } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Button, Dialog, TextField, ViewTabs } from '../../components/ui'
import { useI18n } from '../../i18n'
import { formatAttachmentSize } from '../conversation/attachments/attachmentPresentation'
import { confirmSkillImport, previewGitHubSkills, previewZipSkills, updateSkill } from './api'
import type { ImportPreview, InstalledSkill } from './model'
import { skillError } from './useSkillData'

/** 导入前预览真实内容，失败保留输入，确认只提交已预览的内容摘要 */
export function SkillImportDialog({ trigger, target, onClose, onCompleted }: { trigger: HTMLElement; target?: InstalledSkill; onClose: () => void; onCompleted: (changed: boolean) => void }) {
  const { t } = useI18n()
  const [tab, setTab] = useState<'github' | 'zip'>(target ? 'zip' : 'github')
  const [url, setUrl] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [preview, setPreview] = useState<ImportPreview | null>(null)
  const [selected, setSelected] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [dragging, setDragging] = useState(false)
  const input = useRef<HTMLInputElement>(null)
  const pending = useRef<AbortController | null>(null)
  const attempt = useRef<{ signature: string; id: string } | null>(null)
  useEffect(() => () => pending.current?.abort(), [])
  const chooseFile = (next?: File) => {
    if (!next || busy) return
    setError('')
    if (next.size > 16 * 1024 * 1024) { setError(t('ZIP 文件不能超过 16 MiB')); return }
    setFile(next)
  }
  const submit = async () => {
    if (pending.current) return
    const controller = new AbortController(); pending.current = controller
    setBusy(true); setError('')
    try {
      if (preview) {
        const signature = JSON.stringify([preview.id, [...selected].sort(), target?.id])
        if (attempt.current?.signature !== signature) attempt.current = { signature, id: crypto.randomUUID() }
        const changed = target
          ? (await updateSkill(target.id, attempt.current.id, { draft_id: preview.id, digest: selected[0] }, controller.signal)).changed
          : Boolean(await confirmSkillImport(preview.id, selected, attempt.current.id, controller.signal))
        if (!controller.signal.aborted) { onCompleted(changed); onClose() }
      } else {
        const result = tab === 'github' ? await previewGitHubSkills(url.trim(), controller.signal)
          : await previewZipSkills(file!, controller.signal)
        if (!controller.signal.aborted) {
          const candidates = target ? result.candidates.filter(candidate => candidate.name === target.name) : result.candidates
          if (target && candidates.length !== 1) { setError(t('ZIP 中没有与「{name}」同名的技能', { name: target.name })); return }
          setPreview({ ...result, candidates }); setSelected(candidates.map(candidate => candidate.digest))
        }
      }
    } catch (failure) {
      if (!controller.signal.aborted) setError(skillError(failure, target ? t('更新失败，请重试') : t('导入失败，请重试')))
    } finally {
      if (pending.current === controller) pending.current = null
      if (!controller.signal.aborted) setBusy(false)
    }
  }
  return <Dialog open title={target ? t('更新「{name}」', { name: target.name }) : preview ? t('选择要安装的技能') : t('导入技能')} className="skills-import-dialog" restoreFocusTo={trigger} onClose={onClose} closeDisabled={busy}>
    <div className="skills-import-content">
      {!preview ? <>
        {!target && <ViewTabs value={tab} label={t('导入方式')} options={[{ value: 'github', label: 'GitHub' }, { value: 'zip', label: t('ZIP 文件') }]} onChange={value => { if (!busy) { setTab(value); setError('') } }} />}
        {tab === 'github' ? <TextField label={t('GitHub 地址')} placeholder="https://github.com/owner/repository" value={url} disabled={busy} onChange={event => setUrl(event.target.value)}
          helperText={t('支持公开仓库和技能子目录地址')} rootClassName="skills-import-input" />
          : <div className={`skills-dropzone${dragging ? ' is-dragging' : ''}`} onDragOver={event => { event.preventDefault(); setDragging(true) }} onDragLeave={() => setDragging(false)} onDrop={event => { event.preventDefault(); setDragging(false); chooseFile(event.dataTransfer.files[0]) }}>
            <FileArchive size={30} strokeWidth={1.5} aria-hidden="true" /><strong>{file?.name ?? t('拖入技能 ZIP 文件')}</strong>
            <p>{file ? formatAttachmentSize(file.size) : t('保留 SKILL.md、脚本和资源目录，最大 16 MiB')}</p>
            <input ref={input} type="file" aria-label={t('ZIP 文件')} accept=".zip,application/zip" hidden onChange={event => chooseFile(event.target.files?.[0])} />
            <Button type="button" size="sm" disabled={busy} onClick={() => input.current?.click()}>{file ? t('重新选择') : t('选择文件')}</Button>
          </div>}
        <p className="skills-import-note">{target ? t('选择同名技能的 ZIP，保留启用状态并从下一轮生效') : t('导入到个人技能库，安装后默认启用')}</p>
      </> : <div className="skills-import-candidates" role="group" aria-label={t('可安装技能')}>
        {preview.candidates.map(candidate => {
          const content = <span><strong>{candidate.name}</strong><p>{candidate.description}</p><small>{t('{count} 个文件', { count: candidate.file_count })} · {formatAttachmentSize(candidate.byte_size)}</small></span>
          return target ? <div key={candidate.digest} className="skills-import-candidate">{content}</div>
            : <button type="button" key={candidate.digest} className="skills-import-candidate" role="checkbox" aria-checked={selected.includes(candidate.digest)} disabled={busy}
              onClick={() => setSelected(current => current.includes(candidate.digest) ? current.filter(value => value !== candidate.digest) : [...current, candidate.digest])}>
              <span className="skills-checkbox" aria-hidden="true">{selected.includes(candidate.digest) && <Check size={14} />}</span>{content}
            </button>
        })}
      </div>}
      {error && <p className="skills-field-error" role="alert">{error}</p>}
    </div>
    <footer className="skills-dialog-footer"><Button type="button" variant="ghost" size="sm" disabled={busy} onClick={preview ? () => { setPreview(null); setError('') } : onClose}>{preview ? t('返回') : t('取消')}</Button>
      <Button type="button" variant="primary" size="sm" loading={busy} disabled={preview ? selected.length === 0 : tab === 'github' ? !url.trim() : !file} onClick={() => void submit()}>
        {preview ? target ? t('更新') : t('安装 {count} 项技能', { count: selected.length }) : t('预览技能')}
      </Button>
    </footer>
  </Dialog>
}
