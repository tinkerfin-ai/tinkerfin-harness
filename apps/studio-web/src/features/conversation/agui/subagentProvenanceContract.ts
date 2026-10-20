import type { JsonObject } from "../../../types"
import type { SubagentProvenance } from "../../../api/conversation/types"

export type { SubagentProvenance } from "../../../api/conversation/types"

export const SUBAGENT_PROVENANCE_SCHEMA = "tinkerfin.subagent-provenance" as const

const KEYS = [
  "agentName",
  "description",
  "parentGraphNamespace",
  "parentToolCallId",
  "requestRunId",
  "schema",
  "subagentInvocationId",
] as const

export class SubagentProvenanceContractError extends Error {
  constructor(message: string) {
    super(message)
    this.name = "SubagentProvenanceContractError"
  }
}

const isObject = (value: unknown): value is JsonObject =>
  Boolean(value && typeof value === "object" && !Array.isArray(value))

const canonicalText = (value: unknown): value is string =>
  typeof value === "string" && Boolean(value) && value.trim() === value

const graphNamespace = (value: unknown, field: string): string[] => {
  if (!Array.isArray(value) || !value.every(canonicalText)) {
    throw new SubagentProvenanceContractError(`${field} 无效`)
  }
  return value
}

const parentToolMatchesScope = (id: string, namespace: readonly string[]): boolean => {
  const token = /^tf:tool:([A-Za-z0-9_-]+)$/.exec(id)?.[1]
  if (!token) return false
  try {
    // 父工具 ID 与声明必须指向同一作用域，避免把子任务挂到其他工具上
    const bytes = Uint8Array.from(atob(token.replace(/-/g, "+").replace(/_/g, "/")), character => character.charCodeAt(0))
    const value: unknown = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes))
    return Array.isArray(value)
      && value.length === 2
      && Array.isArray(value[0])
      && value[0].length === namespace.length
      && value[0].every((part: unknown, index: number) => part === namespace[index])
      && typeof value[1] === "string"
      && value[1].length > 0
  } catch {
    return false
  }
}

export const parseSubagentProvenance = (value: unknown): SubagentProvenance => {
  if (!isObject(value)) throw new SubagentProvenanceContractError("subagent provenance 必须是对象")
  const actualKeys = Object.keys(value).sort()
  const expectedKeys = [...KEYS].sort()
  if (
    actualKeys.length !== expectedKeys.length
    || !actualKeys.every((key, index) => key === expectedKeys[index])
  ) throw new SubagentProvenanceContractError("subagent provenance 字段不符合当前契约")
  const parentGraphNamespace = graphNamespace(value.parentGraphNamespace, "parentGraphNamespace")
  if (
    value.schema !== SUBAGENT_PROVENANCE_SCHEMA
    || !canonicalText(value.subagentInvocationId)
    || !/^subagent-[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value.subagentInvocationId)
    || !canonicalText(value.agentName)
    || !canonicalText(value.parentToolCallId)
    || !parentToolMatchesScope(value.parentToolCallId, parentGraphNamespace)
    || typeof value.description !== "string"
    || value.description.length === 0
    || !canonicalText(value.requestRunId)
  ) throw new SubagentProvenanceContractError("subagent provenance 关联字段不一致")
  return {
    schema: SUBAGENT_PROVENANCE_SCHEMA,
    subagentInvocationId: value.subagentInvocationId,
    parentGraphNamespace,
    agentName: value.agentName,
    parentToolCallId: value.parentToolCallId,
    description: value.description,
    requestRunId: value.requestRunId,
  }
}
