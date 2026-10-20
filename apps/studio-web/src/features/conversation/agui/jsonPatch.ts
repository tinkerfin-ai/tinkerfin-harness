import type { StateDeltaOperation } from '../../../api/conversation/types'
import type { DeepReadonly, JsonObject, JsonValue } from '../../../types'

const ARRAY_INDEX = /^(?:0|[1-9]\d*)$/

/** STATE_DELTA 无法按 Studio 状态契约原子应用 */
export class InvalidStateDeltaError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'InvalidStateDeltaError'
  }
}

const fail = (message: string): never => {
  throw new InvalidStateDeltaError(message)
}

const decodePointer = (path: string) => {
  if (path === '') return []
  if (!path.startsWith('/')) fail('JSON Patch path 必须是 JSON Pointer')
  return path.slice(1).split('/').map((token) => {
    let decoded = ''
    for (let index = 0; index < token.length; index += 1) {
      const character = token[index]
      if (character !== '~') {
        decoded += character
        continue
      }
      const escaped = token[index + 1]
      if (escaped !== '0' && escaped !== '1') fail('JSON Pointer 包含非法转义')
      decoded += escaped === '0' ? '~' : '/'
      index += 1
    }
    return decoded
  })
}

const isContainer = (value: unknown): value is JsonObject | JsonValue[] => (
  value !== null && typeof value === 'object'
)

const requireContainer = (value: unknown): JsonObject | JsonValue[] => {
  if (!isContainer(value)) throw new InvalidStateDeltaError('JSON Patch 路径不是容器')
  return value
}

const operationValue = (operation: StateDeltaOperation): JsonValue => {
  if (operation.op === 'remove') throw new InvalidStateDeltaError('remove 操作没有 value')
  return operation.value
}

const arrayIndex = (
  token: string,
  length: number,
  operation: StateDeltaOperation['op'],
) => {
  if (token === '-') {
    if (operation !== 'add') fail('只有 add 可以使用数组追加索引')
    return length
  }
  if (!ARRAY_INDEX.test(token)) fail('JSON Patch 数组索引格式非法')
  const index = Number(token)
  if (!Number.isSafeInteger(index)) fail('JSON Patch 数组索引超出安全范围')
  const maximum = operation === 'add' ? length : length - 1
  if (index > maximum) fail('JSON Patch 数组索引越界')
  return index
}

const ownValue = (target: JsonObject, key: string): JsonValue => {
  if (!Object.hasOwn(target, key)) fail('JSON Patch 路径不存在')
  return Reflect.get(target, key) as JsonValue
}

const defineOwnValue = (target: JsonObject, key: string, value: JsonValue) => {
  Object.defineProperty(target, key, {
    configurable: true,
    enumerable: true,
    value: structuredClone(value),
    writable: true,
  })
}

/**
 * 原子应用 Studio 支持的 RFC 6902 操作
 *
 * Args:
 *   current: 当前对象形态的 AG-UI 状态
 *   delta: 已通过事件结构校验的 Patch 操作
 *
 * Returns:
 *   应用完整批次后的新状态
 *
 * Raises:
 *   InvalidStateDeltaError: Pointer、目标或结果不符合 Studio 状态契约
 */
export function applyStateDelta(
  current: DeepReadonly<JsonObject> | undefined,
  delta: readonly StateDeltaOperation[],
): JsonObject {
  // 共享的历史状态只读；整个批次仅修改本次复制出的工作对象
  let next: JsonValue = requireContainer(structuredClone(current ?? {}))

  for (const operation of delta) {
    const tokens = decodePointer(operation.path)
    if (tokens.length === 0) {
      if (operation.op === 'remove') fail('Studio 状态根不能被删除')
      const rootValue = operationValue(operation)
      if (!isContainer(rootValue) || Array.isArray(rootValue)) {
        fail('Studio 状态根必须是对象')
      }
      next = structuredClone(rootValue)
      continue
    }

    let target = requireContainer(next)
    for (const token of tokens.slice(0, -1)) {
      const child = Array.isArray(target)
        ? target[arrayIndex(token, target.length, 'replace')]
        : ownValue(target, token)
      target = requireContainer(child)
    }

    const key = tokens.at(-1)
    if (key == null) throw new InvalidStateDeltaError('JSON Patch 缺少目标路径')
    if (Array.isArray(target)) {
      const index = arrayIndex(key, target.length, operation.op)
      if (operation.op === 'remove') target.splice(index, 1)
      else if (operation.op === 'add') target.splice(index, 0, structuredClone(operation.value))
      else target[index] = structuredClone(operation.value)
      continue
    }

    if (operation.op === 'remove') {
      if (!Object.hasOwn(target, key)) fail('JSON Patch remove 目标不存在')
      Reflect.deleteProperty(target, key)
    } else if (operation.op === 'replace') {
      if (!Object.hasOwn(target, key)) fail('JSON Patch replace 目标不存在')
      defineOwnValue(target, key, operation.value)
    } else {
      defineOwnValue(target, key, operation.value)
    }
  }

  if (!isContainer(next) || Array.isArray(next)) {
    throw new InvalidStateDeltaError('Studio 状态根必须是对象')
  }
  return next
}
