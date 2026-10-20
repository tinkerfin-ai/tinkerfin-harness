import { afterEach, expect, it, vi } from 'vitest'
import { clearSettingsLocation, clearWorkspaceLocation, normalizeAppLocation, readDeviceSettingsFromLocation, writeDeviceSettingsToLocation } from '../../lib/threadRoute'
import { openDeviceClient, prepareDeviceAuthorization } from './authorizationRoute'

afterEach(() => { vi.restoreAllMocks(); window.history.replaceState(null, '', '/'); sessionStorage.clear() })

it('授权地址不会保存为可恢复页面，离开时清除授权能力', () => {
  window.history.replaceState(null, '', '/?project=project&thread=thread')
  normalizeAppLocation()
  window.history.replaceState(null, '', `/?device_authorization=aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa#token=${'c'.repeat(64)}`)
  normalizeAppLocation()
  expect(sessionStorage.getItem('tinkerfin:last-valid-location')).toBe('/?project=project&thread=thread')
  writeDeviceSettingsToLocation()
  expect(window.location.search).toBe('?settings=devices')
  expect(window.location.hash).toBe('')
  expect(sessionStorage.getItem('tinkerfin:last-valid-location')).toBe('/?settings=devices')
  clearWorkspaceLocation()
  expect(readDeviceSettingsFromLocation()).toBe(true)
  clearSettingsLocation()
  expect(window.location.search).toBe('')
})

it('无效授权入口清理后进入设置目的地，打开客户端不携带旧授权身份', () => {
  const open = vi.spyOn(window, 'open').mockReturnValue(null)
  window.history.replaceState(null, '', '/?device_authorization=invalid&project=private#token=invalid')
  prepareDeviceAuthorization()
  expect(window.location.search).toBe('?settings=devices')
  expect(window.location.hash).toBe('')
  expect(open).toHaveBeenCalledWith('tinkerfin://connect', '_self')
})

it('历史缓存中的授权入口不能在未知路径被重新打开', () => {
  sessionStorage.setItem('tinkerfin:last-valid-location', '/?device_authorization=old#token=secret')
  window.history.replaceState(null, '', '/unknown')
  normalizeAppLocation()
  expect(window.location.pathname + window.location.search + window.location.hash).toBe('/')
})

it('浏览器拒绝唤起客户端时不阻断网页恢复', () => {
  const open = vi.spyOn(window, 'open').mockImplementation(() => { throw new Error('blocked') })
  expect(() => openDeviceClient()).not.toThrow()
  expect(open).toHaveBeenCalledWith('tinkerfin://connect', '_self')
})
