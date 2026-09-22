export const DEFAULT_SERVER_ADDRESS = 'http://127.0.0.1:8090'
export const SERVER_ADDRESS_STORAGE_KEY = 'tinkerfin.server-address'
const SERVER_ADDRESS_CHANGED = 'tinkerfin:server-address-changed'

/** 地址不可用时阻止请求，避免悄悄连接其他服务器 */
export class ServerAddressError extends Error {
  constructor(readonly reason: 'invalid' | 'storage', cause?: unknown) {
    super(reason === 'invalid' ? '服务器地址无效' : '无法访问服务器地址配置', { cause })
    this.name = 'ServerAddressError'
  }
}

export function normalizeServerAddress(value: string): string {
  if (!value.trim()) return DEFAULT_SERVER_ADDRESS
  try {
    const url = new URL(value.trim())
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
      throw new Error('invalid-server-address')
    }
    return url.href.replace(/\/+$/, '')
  } catch (error) {
    throw new ServerAddressError('invalid', error)
  }
}

export function getServerAddress(): string {
  let stored: string | null
  try { stored = window.localStorage.getItem(SERVER_ADDRESS_STORAGE_KEY) }
  catch (error) { throw new ServerAddressError('storage', error) }
  return normalizeServerAddress(stored ?? '')
}

export function setServerAddress(value: string): string {
  const address = normalizeServerAddress(value)
  let previous: string | null
  try {
    previous = window.localStorage.getItem(SERVER_ADDRESS_STORAGE_KEY)
    if (address === DEFAULT_SERVER_ADDRESS) window.localStorage.removeItem(SERVER_ADDRESS_STORAGE_KEY)
    else window.localStorage.setItem(SERVER_ADDRESS_STORAGE_KEY, address)
  } catch (error) { throw new ServerAddressError('storage', error) }
  if ((previous || DEFAULT_SERVER_ADDRESS) !== address) window.dispatchEvent(new Event(SERVER_ADDRESS_CHANGED))
  return address
}

export function subscribeServerAddress(listener: () => void): () => void {
  const onStorage = (event: StorageEvent) => {
    if (event.key === SERVER_ADDRESS_STORAGE_KEY || event.key === null) listener()
  }
  window.addEventListener('storage', onStorage)
  window.addEventListener(SERVER_ADDRESS_CHANGED, listener)
  return () => {
    window.removeEventListener('storage', onStorage)
    window.removeEventListener(SERVER_ADDRESS_CHANGED, listener)
  }
}

export function buildApiUrl(path: string): string {
  return /^https?:\/\//i.test(path) ? path : `${getServerAddress()}${path.startsWith('/') ? path : `/${path}`}`
}
