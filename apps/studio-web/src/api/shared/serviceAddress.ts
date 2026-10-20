export const LOCAL_STUDIO_API_ADDRESS = 'http://127.0.0.1:8090'

export function parseServiceAddress(value: string): string {
  const url = new URL(value.trim())
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
    throw new Error('invalid-server-address')
  }
  return url.href.replace(/\/+$/, '')
}
