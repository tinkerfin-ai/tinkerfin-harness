import type { TranslationKey } from '../../i18n'

export const modelDiscoveryMessages: Record<string, TranslationKey> = {
  models_unavailable: '无法获取模型列表，请重试或手动添加',
  invalid_response: '服务返回格式不符合当前接口要求',
  response_too_large: '模型列表超过大小限制',
  timeout: '获取模型列表超时，请重试',
  authentication_failed: '服务拒绝了认证，请检查密钥及接口权限',
  rate_limited: '服务请求受限，请稍后重试',
  service_error: '模型服务返回错误，请检查配置和服务状态',
  network_error: '无法连接模型服务，请检查地址和网络',
  endpoint_not_allowed: '该地址不在管理员允许的访问范围内',
}
