import { useEffect, useRef, useState } from 'react'
import { DEFAULT_SERVER_ADDRESS, getServerAddress, ServerAddressError, setServerAddress, subscribeServerAddress } from '../../api/shared/config'
import { useI18n } from '../../i18n'

/** 地址草稿允许暂时不完整；只有合法值持久化，提交时必须完成校验 */
export function useServerAddress(onError: (message: string) => void) {
  const { t } = useI18n()
  const [draft, setDraft] = useState('')
  const [error, setError] = useState<string>()
  const saving = useRef(false)
  const lastStorageError = useRef(false)
  const report = useRef(onError)
  report.current = onError

  useEffect(() => {
    const read = () => {
      if (saving.current) return
      try {
        const address = getServerAddress()
        setDraft(address === DEFAULT_SERVER_ADDRESS ? '' : address)
        setError(undefined)
      } catch {
        report.current(t('无法读取服务器地址，请检查浏览器存储设置'))
      }
    }
    read()
    return subscribeServerAddress(read)
  }, [t])

  const persist = (value: string, validate: boolean) => {
    saving.current = true
    try {
      const address = setServerAddress(value)
      setError(undefined)
      lastStorageError.current = false
      if (validate) setDraft(address === DEFAULT_SERVER_ADDRESS ? '' : address)
      return true
    } catch (reason) {
      if (reason instanceof ServerAddressError && reason.reason === 'invalid') {
        if (validate) setError(t('请输入完整的 HTTP(S) 服务器地址，不含账号、查询参数或片段'))
      } else if (!lastStorageError.current) {
        lastStorageError.current = true
        report.current(t('浏览器无法保存服务器地址，请检查存储设置后重试'))
      }
      return false
    } finally { saving.current = false }
  }
  return {
    draft,
    error,
    change(value: string) { setDraft(value); setError(undefined); persist(value, false) },
    validate() { return persist(draft, true) },
  }
}
