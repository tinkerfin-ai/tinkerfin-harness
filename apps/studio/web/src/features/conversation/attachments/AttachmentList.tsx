import { CornerDownLeft, Download, RotateCcw } from 'lucide-react'
import { useContext, useState } from 'react'
import type { CSSProperties } from 'react'
import { Button, ErrorBoundary, IconButton } from '../../../components/ui'
import { useI18n } from '../../../i18n'
import type { Attachment } from './content'
import { AttachmentReferenceContext } from './context'
import { AttachmentViewer } from './AttachmentViewer'
import { useAttachmentImage } from './useAttachmentImage'
import { useAttachmentDownload } from './useAttachmentDownload'
import { documentFormat } from './documentPreview'
import { AttachmentFileIcon } from './AttachmentFileIcon'
import { formatAttachmentSize } from './attachmentPresentation'
import './attachments.css'

function AttachmentCard({
  attachment,
  onPreview,
}: {
  attachment: Attachment
  onPreview: (attachment: Attachment, url: string, target: HTMLElement) => void
}) {
  const { t } = useI18n()
  const reference = useContext(AttachmentReferenceContext)
  const isImage = attachment.mime_type.startsWith('image/')
  const image = useAttachmentImage(isImage ? attachment.id : undefined)
  const download = useAttachmentDownload(attachment)
  const [ratio, setRatio] = useState(1)
  const documentPreview = documentFormat(attachment.mime_type)
  const actions = (
    <>
      {reference && (
        <IconButton
          type="button"
          variant="ghost"
          label={t('引用附件：{name}', { name: attachment.name })}
          tooltip={t(isImage ? '引用图片' : '引用附件')}
          icon={<CornerDownLeft size={18} />}
          disabled={isImage && image.failed}
          onClick={() => reference(attachment)}
        />
      )}
      <IconButton
        type="button"
        variant="ghost"
        label={t('下载附件：{name}', { name: attachment.name })}
        tooltip={t(isImage ? '下载图片' : '下载附件')}
        icon={<Download size={18} />}
        disabled={isImage && image.failed}
        loading={download.pending}
        onClick={() => void download.download()}
      />
    </>
  )
  return (
    <div
      className={`attachment-card${isImage ? ' attachment-card--image' : ''}`}
      style={{ '--attachment-ratio': ratio } as CSSProperties}
    >
      {isImage ? (
        <div
          className="attachment-media"
          data-state={image.failed ? 'error' : image.url ? 'ready' : 'loading'}
        >
          <button
            type="button"
            className="attachment-preview"
            disabled={!image.url}
            aria-label={t('放大图片：{name}', { name: attachment.name })}
            onClick={(event) => {
              if (image.url)
                onPreview(attachment, image.url, event.currentTarget)
            }}
          >
            {image.url ? (
              <img
                src={image.url}
                alt={attachment.name}
                onError={image.fail}
                onLoad={(event) =>
                  setRatio(
                    event.currentTarget.naturalWidth /
                      event.currentTarget.naturalHeight,
                  )
                }
              />
            ) : (
              <span className="attachment-loading" role="status">
                {image.failed
                  ? t('图片暂时无法打开，请重试')
                  : t('正在加载图片…')}
              </span>
            )}
          </button>
          {(image.url || image.failed) && (
            <div
              className="attachment-image-actions"
              role="group"
              aria-label={t('图片操作')}
            >
              {image.failed && (
                <IconButton
                  type="button"
                  variant="ghost"
                  label={t('重试附件')}
                  tooltip={t('重试附件')}
                  icon={<RotateCcw size={18} />}
                  onClick={image.retry}
                />
              )}
              {actions}
            </div>
          )}
        </div>
      ) : (
        <div className="attachment-description">
          <button
            type="button"
            className="attachment-document-preview"
            aria-label={t(documentPreview ? '预览文档：{name}' : '查看文件：{name}', { name: attachment.name })}
            onClick={event => onPreview(attachment, '', event.currentTarget)}
          >
            <AttachmentFileIcon attachment={attachment} />
            <span className="attachment-description__text">
              <strong>{attachment.name}</strong>
              <small>{formatAttachmentSize(attachment.size_bytes)}</small>
            </span>
          </button>
          <div
            className="attachment-file-actions"
            role="group"
            aria-label={t('附件操作')}
          >
            {actions}
          </div>
        </div>
      )}
      {download.failed && (
        <div className="attachment-error" role="status">
          {t('下载失败，请重试')}
          <Button
            type="button"
            variant="ghost"
            onClick={() => void download.download()}
            loading={download.pending}
          >
            {t('重试下载')}
          </Button>
        </div>
      )}
    </div>
  )
}

export function AttachmentList({
  attachments,
}: {
  attachments?: readonly Attachment[]
}) {
  const { t } = useI18n()
  const [preview, setPreview] = useState<{
    attachment: Attachment
    url: string
    target: HTMLElement
  }>()
  if (!attachments?.length) return null
  return (
    <>
      <div
        className={`message-attachments${attachments.length > 1 ? ' message-attachments--multiple' : ''}`}
      >
        {attachments.map((attachment) => (
          <ErrorBoundary
            key={attachment.id}
            resetKey={attachment.id}
            fallback={({ reset }) => (
              <div className="attachment-error" role="status">
                {t('附件暂时无法打开，请重试')}
                <Button type="button" onClick={reset}>
                  {t('重试附件')}
                </Button>
              </div>
            )}
          >
            <AttachmentCard
              attachment={attachment}
              onPreview={(item, url, target) =>
                setPreview({ attachment: item, url, target })
              }
            />
          </ErrorBoundary>
        ))}
      </div>
      {preview && (
        <ErrorBoundary
          fallback={({ reset }) => (
            <div className="attachment-error" role="status">
              {t('附件暂时无法打开，请重试')}
              <Button
                type="button"
                onClick={() => {
                  setPreview(undefined)
                  reset()
                }}
              >
                {t('关闭预览')}
              </Button>
            </div>
          )}
        >
          <AttachmentViewer
            attachments={preview.attachment.mime_type.startsWith('image/')
              ? attachments.filter(item => item.mime_type.startsWith('image/'))
              : attachments.filter(item => !item.mime_type.startsWith('image/'))}
            initialId={preview.attachment.id}
            initialPreview={preview.url}
            returnFocus={preview.target}
            onClose={() => setPreview(undefined)}
          />
        </ErrorBoundary>
      )}
    </>
  )
}
