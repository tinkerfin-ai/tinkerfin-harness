import type { Attachment } from './content'
import { AttachmentFileIcon } from './AttachmentFileIcon'

export function AttachmentViewerTitle({ attachment }: { attachment: Attachment }) {
  return (
    <span className="attachment-document-title">
      <AttachmentFileIcon attachment={attachment} compact />
      <span className="attachment-document-title__copy">
        <span>{attachment.name}</span>
      </span>
    </span>
  )
}
