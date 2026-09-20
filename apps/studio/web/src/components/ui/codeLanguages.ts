const aliases: Record<string, string> = {
  js: 'javascript', ts: 'typescript', py: 'python', sh: 'bash', shell: 'bash',
  yml: 'yaml', html: 'markup', xml: 'markup', svg: 'markup', md: 'markdown',
  cs: 'csharp', 'c++': 'cpp', jsonc: 'json',
}
const languages = new Set([
  'javascript', 'typescript', 'jsx', 'tsx', 'json', 'python', 'bash', 'yaml',
  'markup', 'css', 'sql', 'markdown', 'mermaid', 'java', 'c', 'cpp', 'csharp',
  'go', 'rust', 'diff',
])

export function codeLanguage(value = ''): string | undefined {
  const name = value.toLowerCase()
  const language = aliases[name] ?? name
  return languages.has(language) ? language : undefined
}
