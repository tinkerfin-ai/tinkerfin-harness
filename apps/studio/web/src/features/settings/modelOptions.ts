import { createScanner, parseTree, type Node, type ParseError } from 'jsonc-parser'
import type { JsonObject } from '../../types'

export const IMAGE_OUTPUT_FORMATS = ['png', 'jpeg', 'webp'] as const
export type ImageOutputFormat = typeof IMAGE_OUTPUT_FORMATS[number]

export const MAX_MODEL_OPTIONS_BYTES = 64 * 1024

export type OptionsIssueCode = 'too_large' | 'too_deep' | 'syntax' | 'object_required' | 'duplicate' | 'reserved' | 'nonfinite'
export interface OptionsIssue { code: OptionsIssueCode; from: number; to: number; key?: string }
export type ParsedOptions = { value: JsonObject; issues: [] } | { value: null; issues: OptionsIssue[] }

/** 校验高级参数；错误位置以文本偏移表示，供编辑器和表单共用 */
export function parseServiceOptions(text: string, reservedKeys: readonly string[] = []): ParsedOptions {
  const reserved = new Set(reservedKeys)
  if (new TextEncoder().encode(text).length > MAX_MODEL_OPTIONS_BYTES)
    return { value: null, issues: [{ code: 'too_large', from: 0, to: text.length }] }
  const scanner = createScanner(text, true)
  let depth = 0
  for (scanner.scan(); scanner.getTokenLength() > 0; scanner.scan()) {
    const token = text.slice(scanner.getTokenOffset(), scanner.getTokenOffset() + scanner.getTokenLength())
    if (token === '{' || token === '[') depth += 1
    if (depth > 32) return { value: null, issues: [{ code: 'too_deep', from: scanner.getTokenOffset(), to: scanner.getTokenOffset() + 1 }] }
    if (token === '}' || token === ']') depth -= 1
  }
  const errors: ParseError[] = []
  const tree = parseTree(text, errors, { allowTrailingComma: false, disallowComments: true })
  if (errors.length || !tree)
    return { value: null, issues: errors.length ? errors.map((error) => ({ code: 'syntax', from: error.offset, to: error.offset + Math.max(1, error.length) })) : [{ code: 'syntax', from: 0, to: text.length }] }
  if (tree.type !== 'object') return { value: null, issues: [{ code: 'object_required', from: tree.offset, to: tree.offset + tree.length }] }
  const issues: OptionsIssue[] = []
  const visit = (node: Node, top: boolean) => {
    if (node.type === 'object') {
      const seen = new Set<string>()
      for (const property of node.children ?? []) {
        const keyNode = property.children?.[0]
        const valueNode = property.children?.[1]
        const key: string = keyNode?.value ?? ''
        const report = (code: OptionsIssueCode) => issues.push({ code, key, from: keyNode?.offset ?? property.offset, to: (keyNode?.offset ?? property.offset) + (keyNode?.length ?? property.length) })
        if (seen.has(key)) report('duplicate')
        seen.add(key)
        if ((top && reserved.has(key.toLowerCase())) || ['apikey', 'authorization', 'accesstoken'].includes(key.toLowerCase().replace(/[_-]/g, ''))) report('reserved')
        if (valueNode) visit(valueNode, false)
      }
    } else if (node.type === 'number' && !Number.isFinite(node.value)) {
      issues.push({ code: 'nonfinite', from: node.offset, to: node.offset + node.length })
    } else {
      for (const child of node.children ?? []) visit(child, false)
    }
  }
  visit(tree, true)
  if (issues.length) return { value: null, issues }
  return { value: JSON.parse(text) as JsonObject, issues: [] }
}
