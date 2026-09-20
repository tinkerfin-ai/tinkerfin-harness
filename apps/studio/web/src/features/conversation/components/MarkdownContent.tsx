import {
  Children,
  isValidElement,
  memo,
  useContext,
  useMemo,
} from 'react'
import type { ReactNode } from 'react'
import type { ComponentPropsWithoutRef } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

import { SourceCodeBlock } from './SourceCodeBlock'
import { MarkdownStreamingContext } from './MarkdownStreamingContext'
import { MermaidBlock } from '../diagrams/MermaidBlock'
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

function CodeBlock({ children }: { children?: ReactNode }) {
  const isStreaming = useContext(MarkdownStreamingContext)
  const child = Children.count(children) === 1 ? Children.only(children) : children
  const className = isValidElement<{ className?: string }>(child) ? child.props.className : undefined
  const language = className?.match(/language-([^\s]+)/)?.[1]
  const code = textFromNode(child).replace(/\n$/, '')
  if (language?.toLowerCase() === 'mermaid') return <MermaidBlock source={code} isStreaming={isStreaming} />
  return <SourceCodeBlock source={code} language={language} isStreaming={isStreaming} />
}

function MarkdownContentView({
  content,
  className,
  variant = 'article',
  allowRemoteImages = true,
  isStreaming = false,
}: {
  content: string
  className?: string
  variant?: MarkdownVariant
  allowRemoteImages?: boolean
  isStreaming?: boolean
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
    pre: CodeBlock,
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
      <MarkdownStreamingContext.Provider value={isStreaming}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {content}
      </ReactMarkdown>
      </MarkdownStreamingContext.Provider>
    </div>
  )
}

export const MarkdownContent = memo(MarkdownContentView)
