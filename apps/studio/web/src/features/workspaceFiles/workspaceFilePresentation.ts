export function formatWorkspaceFileSize(bytes: number | null, locale: string) {
  if (bytes === null) return '—'
  if (bytes < 1024) return `${bytes.toLocaleString(locale)} B`
  const unit = bytes < 1024 * 1024 ? 'KB' : 'MB'
  const value = bytes / (unit === 'KB' ? 1024 : 1024 * 1024)
  return `${value.toLocaleString(locale, { maximumFractionDigits: 1 })} ${unit}`
}

export function workspaceFileFormat(name: string) {
  return (name.includes('.') ? name.split('.').at(-1)?.toUpperCase() : undefined) || 'FILE'
}
