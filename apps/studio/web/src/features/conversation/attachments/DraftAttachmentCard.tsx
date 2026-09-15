import { FileImage, RotateCcw, X } from 'lucide-react'
import { IconButton } from '../../../components/ui'
import { FeedbackIcon } from '../../../components/ui/FeedbackState'
import { isTranslationKey, useI18n } from '../../../i18n'
import type { DraftAttachment } from '../useAttachments'
import { useAttachmentImage } from './useAttachmentImage'
import { AttachmentFilename } from './AttachmentFilename'
import { AttachmentFileIcon } from './AttachmentFileIcon'
import './attachments.css'

export function DraftAttachmentCard({
  attachment,
  onRemove,
  onRetry,
}: {
  attachment: DraftAttachment
  onRemove: (id: string) => void
  onRetry?: (id: string) => void
}) {
  const { t } = useI18n()
  const pending = attachment.state === 'queued' || attachment.state === 'uploading'
  const failed = attachment.state === 'error'
  const error = attachment.error ?? '上传失败，请重试'
  const image = useAttachmentImage(
    attachment.kind === 'image' ? attachment.attachment?.id : undefined,
    'preview',
    attachment.kind === 'image' ? attachment.file : undefined,
  )
  const fileIconAttachment = attachment.attachment ?? {
    id: attachment.id,
    name: attachment.name,
    mime_type: attachment.file?.type ?? '',
    size_bytes: attachment.size,
  }
  return (
    <div
      className={`composer-attachment${attachment.kind === 'image' ? ' composer-attachment--image' : ''}`}
      data-state={attachment.state}
      role="group"
      aria-label={attachment.name}
      aria-busy={pending || undefined}
    >
      {pending ? <FeedbackIcon kind="loading" /> : image.url ? (
        <img
          className="composer-attachment-thumbnail"
          src={image.url}
          alt={attachment.name}
          onError={image.fail}
        />
      ) : (
        <span className="composer-attachment-icon" aria-hidden="true">
          {attachment.kind === 'image' ? (
            <FileImage size={16} />
          ) : (
            <AttachmentFileIcon attachment={fileIconAttachment} compact />
          )}
        </span>
      )}
      <div className="composer-attachment-copy">
        <AttachmentFilename name={attachment.name} />
        {pending && (
          <span
            className="composer-attachment-state"
            role="status"
          >
            {attachment.state === 'queued'
              ? t('等待上传')
              : t('上传中 {progress}%', { progress: attachment.progress })}
          </span>
        )}
        {failed && (
          <span
            className="composer-attachment-state"
            role="status"
            title={isTranslationKey(error) ? t(error) : error}
          >
            {t('上传失败')}
          </span>
        )}
      </div>
      <div className="composer-attachment-actions">
        {image.failed && (
          <IconButton
            type="button"
            variant="ghost"
            label={t('重新加载缩略图')}
            icon={<RotateCcw size={12} />}
            onClick={image.retry}
          />
        )}
        {failed && onRetry && (
          <IconButton
            type="button"
            variant="ghost"
            label={t('重试附件：{name}', { name: attachment.name })}
            tooltip={t('重试附件')}
            icon={<RotateCcw size={12} />}
            onClick={() => onRetry(attachment.id)}
          />
        )}
        <IconButton
          type="button"
          variant="ghost"
          label={t('移除附件：{name}', { name: attachment.name })}
          icon={<X size={16} />}
          onClick={() => onRemove(attachment.id)}
        />
      </div>
    </div>
  )
}
