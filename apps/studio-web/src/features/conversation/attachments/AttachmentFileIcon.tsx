import type { Attachment } from './content'
import { attachmentFileType } from './attachmentPresentation'

export function AttachmentFileIcon({
  attachment,
  compact = false,
}: {
  attachment: Attachment
  compact?: boolean
}) {
  const fileType = attachmentFileType(attachment)
  return (
    <span
      className={`attachment-file-icon${compact ? ' attachment-file-icon--compact' : ''}`}
      data-file-kind={fileType.kind}
      aria-hidden="true"
    >
      <span>{fileType.label}</span>
    </span>
  )
}
