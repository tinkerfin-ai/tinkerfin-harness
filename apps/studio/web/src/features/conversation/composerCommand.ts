export type ComposerSubmission =
  | { kind: 'message'; content: string }
  | { kind: 'plan-message'; content: string }
  | { kind: 'plan-off-unsupported' }
  | { kind: 'compact' }
  | { kind: 'compact-arguments-unsupported' }

export const parseComposerSubmission = (value: string): ComposerSubmission | null => {
  const input = value.trim()
  if (input === '/compact') return { kind: 'compact' }
  if (/^\/compact\s/.test(input)) return { kind: 'compact-arguments-unsupported' }
  if (input === '/plan') return null
  if (!input.startsWith('/plan') || !/\s/.test(input[5] ?? '')) {
    return { kind: 'message', content: input }
  }
  const content = input.slice(5).trim()
  if (content === 'off') return { kind: 'plan-off-unsupported' }
  return content
    ? { kind: 'plan-message', content }
    : null
}
