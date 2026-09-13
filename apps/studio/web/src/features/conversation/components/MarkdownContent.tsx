import { Check, Copy, TriangleAlert } from 'lucide-react'
import {
  Children,
  isValidElement,
  memo,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import type { ReactNode } from 'react'
import type { ComponentPropsWithoutRef } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

import { Button } from '../../../components/ui'
import { COPY_FEEDBACK_DURATION_MS } from './copyFeedback'
import { useI18n } from '../../../i18n'

export type MarkdownVariant = 'article' | 'compact'

const bareAsciiUrl = /^(https?:\/\/[A-Za-z0-9.-]+(?::\d+)?(?:[/?#][A-Za-z0-9\-._~:/?#[\]@!$&'()*+,;=%]*)?)([\s\S]*)$/i
const bareUrlClassName = 'markdown-bare-url'

function splitBareUrl(children: ReactNode) {
  const text = typeof children === 'string'
    ? children
    : Array.isArray(children) && children.every((child) => typeof child === 'string')
      ? children.join('')
      : null
  if (text == null) return null
  const match = text.match(bareAsciiUrl)
  if (!match || !match[2]) return null

  const trailingPunctuation = match[1].match(/[.,!?;:]+$/)?.[0] ?? ''
  return {
    literal: text,
    url: trailingPunctuation ? match[1].slice(0, -trailingPunctuation.length) : match[1],
    suffix: `${trailingPunctuation}${match[2]}`,
  }
}

function isLiteralAutolink(href: string | undefined, literal: string) {
  if (!href?.startsWith('http')) return false
  try {
    return decodeURI(href) === literal
  } catch {
    return href === literal
  }
}

function externalLinkProps(href: string | undefined) {
  return href && /^https?:\/\//i.test(href)
    ? { target: '_blank', rel: 'noopener noreferrer' }
    : {}
}

function textFromNode(node: ReactNode): string {
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textFromNode).join('')
  if (isValidElement<{ children?: ReactNode }>(node)) return textFromNode(node.props.children)
  return ''
}

type CopyState = 'idle' | 'copied' | 'failed'

function CodeBlock({ children }: { children: ReactNode }) {
  const { t } = useI18n()
  const child = Children.count(children) === 1
    ? Children.only(children)
    : children
  const className = isValidElement<{ className?: string }>(child)
    ? child.props.className
    : undefined
  const language = className?.match(/language-([^\s]+)/)?.[1]
  const code = textFromNode(child).replace(/\n$/, '')
  const [copyState, setCopyState] = useState<CopyState>('idle')
  const resetTimer = useRef<number | null>(null)

  useEffect(() => () => {
    if (resetTimer.current != null) window.clearTimeout(resetTimer.current)
  }, [])

  const copyCode = async () => {
    if (resetTimer.current != null) window.clearTimeout(resetTimer.current)
    try {
      await navigator.clipboard.writeText(code)
      setCopyState('copied')
    } catch {
      setCopyState('failed')
    }
    resetTimer.current = window.setTimeout(() => setCopyState('idle'), COPY_FEEDBACK_DURATION_MS)
  }

  return (
    <figure className="markdown-code-block">
      <figcaption className="markdown-code-block__head">
        <span>{language || t('文本')}</span>
        <Button
          variant="ghost"
          size="sm"
          className="markdown-copy-button"
          leadingIcon={copyState === 'copied'
            ? <Check size={14} />
            : copyState === 'failed'
              ? <TriangleAlert size={14} />
              : <Copy size={14} />}
          onClick={() => void copyCode()}
        >
          {copyState === 'copied' ? t('已复制') : copyState === 'failed' ? t('复制失败') : t('复制')}
        </Button>
      </figcaption>
      <pre>{children}</pre>
    </figure>
  )
}

function MarkdownContentView({
  content,
  className,
  variant = 'article',
  allowRemoteImages = true,
}: {
  content: string
  className?: string
  variant?: MarkdownVariant
  allowRemoteImages?: boolean
}) {
  const { t } = useI18n()
  const components = useMemo(() => ({
    img({ src, alt, title }: ComponentPropsWithoutRef<'img'>) {
      return allowRemoteImages || src?.startsWith('blob:')
        ? <img src={src} alt={alt} title={title} />
        : <span>{alt}</span>
    },
    a({ children, href }: { children?: ReactNode; href?: string }) {
      const bareUrl = splitBareUrl(children)
      if (bareUrl && isLiteralAutolink(href, bareUrl.literal)) {
        return <><a className={bareUrlClassName} href={bareUrl.url} {...externalLinkProps(bareUrl.url)}>{bareUrl.url}</a>{bareUrl.suffix}</>
      }
      const literal = textFromNode(children)
      return (
        <a
          className={isLiteralAutolink(href, literal) ? bareUrlClassName : undefined}
          href={href}
          {...externalLinkProps(href)}
        >
          {children}
        </a>
      )
    },
    table({ children }: { children?: ReactNode }) {
      return (
        <div className="markdown-table-wrap" role="region" tabIndex={0} aria-label={t('可横向滚动的表格')}>
          <table>{children}</table>
        </div>
      )
    },
    pre({ children }: { children?: ReactNode }) {
      return <CodeBlock>{children}</CodeBlock>
    },
    input({ type, ...props }: ComponentPropsWithoutRef<'input'>) {
      return type === 'checkbox'
        ? <input {...props} type={type} aria-hidden="true" tabIndex={-1} />
        : <input {...props} type={type} />
    },
  }), [allowRemoteImages, t])
  const classes = [
    'markdown-content',
    `markdown-content--${variant}`,
    className,
  ].filter(Boolean).join(' ')

  return (
    <div className={classes}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {content}
      </ReactMarkdown>
    </div>
  )
}

export const MarkdownContent = memo(MarkdownContentView)
