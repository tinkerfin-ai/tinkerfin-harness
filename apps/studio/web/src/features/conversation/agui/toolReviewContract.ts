import type { DeepReadonly, JsonObject, JsonValue } from "../../../types"

export const TOOL_REVIEW_SCHEMA = "tinkerfin.deepagents.tool-review" as const

export type ToolReviewDecision = "approve" | "edit" | "reject" | "respond"

export interface ToolReviewInterruptMetadata {
  readonly schema: typeof TOOL_REVIEW_SCHEMA
  readonly nativeInterruptId: string
  readonly actionIndex: number
  readonly toolName: string
  readonly allowedDecisions: readonly ToolReviewDecision[]
  readonly originalArgs: DeepReadonly<JsonObject>
}

interface ToolReviewInterruptLike {
  id: string
  reason: string
  toolCallId?: string | null
  metadata?: DeepReadonly<JsonObject> | null
}

const DECISIONS = new Set<ToolReviewDecision>(["approve", "edit", "reject", "respond"])
const METADATA_KEYS = [
  "actionIndex",
  "allowedDecisions",
  "nativeInterruptId",
  "originalArgs",
  "schema",
  "toolName",
] as const

export class ToolReviewContractError extends Error {
  constructor(message: string) {
    super(message)
    this.name = "ToolReviewContractError"
  }
}

const isJsonValue = (value: unknown): value is DeepReadonly<JsonValue> => {
  if (value === null || typeof value === "string" || typeof value === "boolean") return true
  if (typeof value === "number") return Number.isFinite(value)
  if (Array.isArray(value)) return value.every(isJsonValue)
  if (typeof value !== "object") return false
  return Object.values(value).every(isJsonValue)
}

const isJsonObject = (value: unknown): value is DeepReadonly<JsonObject> =>
  Boolean(value && typeof value === "object" && !Array.isArray(value) && isJsonValue(value))

const hasOnlyKeys = (value: DeepReadonly<JsonObject>, keys: readonly string[]) => {
  const actual = Object.keys(value).sort()
  const expected = [...keys].sort()
  return actual.length === expected.length && actual.every((key, index) => key === expected[index])
}

const jsonValuesEqual = (left: DeepReadonly<JsonValue>, right: DeepReadonly<JsonValue>): boolean => {
  if (left === null || right === null) return left === right
  if (Array.isArray(left) || Array.isArray(right)) {
    return Array.isArray(left)
      && Array.isArray(right)
      && left.length === right.length
      && left.every((item, index) => jsonValuesEqual(item, right[index]))
  }
  if (typeof left === "object" || typeof right === "object") {
    if (!isJsonObject(left) || !isJsonObject(right)) return false
    const leftKeys = Object.keys(left).sort()
    const rightKeys = Object.keys(right).sort()
    return leftKeys.length === rightKeys.length
      && leftKeys.every((key, index) => (
        key === rightKeys[index]
        && jsonValuesEqual(left[key], right[key])
      ))
  }
  return typeof left === typeof right && left === right
}

const parseDecisions = (value: unknown, field: string): ToolReviewDecision[] => {
  if (!Array.isArray(value) || value.length === 0 || !value.every(
    (item): item is ToolReviewDecision => typeof item === "string" && DECISIONS.has(item as ToolReviewDecision),
  )) throw new ToolReviewContractError(`${field} 无效`)
  if (new Set(value).size !== value.length) throw new ToolReviewContractError(`${field} 包含重复值`)
  return value
}

const parseNativeRequest = (value: unknown) => {
  if (!isJsonObject(value) || !hasOnlyKeys(value, ["action_requests", "review_configs"])) {
    throw new ToolReviewContractError("metadata.langgraphValue 无效")
  }
  const actions = value.action_requests
  const policies = value.review_configs
  if (
    !Array.isArray(actions)
    || !Array.isArray(policies)
    || actions.length === 0
    || actions.length !== policies.length
  ) throw new ToolReviewContractError("原生 Tool review 分组无效")

  return actions.map((rawAction, index) => {
    const rawPolicy = policies[index]
    if (!isJsonObject(rawAction) || !isJsonObject(rawPolicy)) {
      throw new ToolReviewContractError("原生 Tool review action 无效")
    }
    const actionKeys = rawAction.description === undefined
      ? ["args", "name"]
      : ["args", "description", "name"]
    const policyKeys = rawPolicy.args_schema === undefined
      ? ["action_name", "allowed_decisions"]
      : ["action_name", "allowed_decisions", "args_schema"]
    if (!hasOnlyKeys(rawAction, actionKeys) || !hasOnlyKeys(rawPolicy, policyKeys)) {
      throw new ToolReviewContractError("原生 Tool review 字段无效")
    }
    if (
      typeof rawAction.name !== "string"
      || !rawAction.name
      || rawAction.name !== rawPolicy.action_name
      || !isJsonObject(rawAction.args)
      || (rawAction.description !== undefined && typeof rawAction.description !== "string")
      || (rawPolicy.args_schema !== undefined && !isJsonObject(rawPolicy.args_schema))
    ) throw new ToolReviewContractError("原生 Tool review action 与 policy 不一致")
    return {
      name: rawAction.name,
      args: rawAction.args,
      allowedDecisions: parseDecisions(rawPolicy.allowed_decisions, "review_configs.allowed_decisions"),
    }
  })
}

export const parseToolReviewInterrupt = (
  interrupt: ToolReviewInterruptLike,
): ToolReviewInterruptMetadata => {
  if (interrupt.reason !== "tool_call") throw new ToolReviewContractError("interrupt reason 必须是 tool_call")
  if (typeof interrupt.toolCallId !== "string" || !interrupt.toolCallId) {
    throw new ToolReviewContractError("Tool review 缺少 toolCallId")
  }
  const metadata = interrupt.metadata
  if (!metadata) throw new ToolReviewContractError("Tool review 缺少 metadata")
  const deepagents = metadata.deepagents
  if (!isJsonObject(deepagents) || !hasOnlyKeys(deepagents, METADATA_KEYS)) {
    throw new ToolReviewContractError("metadata.deepagents 不符合当前契约")
  }
  if (
    deepagents.schema !== TOOL_REVIEW_SCHEMA
    || typeof deepagents.nativeInterruptId !== "string"
    || !deepagents.nativeInterruptId
    || deepagents.nativeInterruptId.trim() !== deepagents.nativeInterruptId
    || !Number.isInteger(deepagents.actionIndex)
    || (deepagents.actionIndex as number) < 0
    || typeof deepagents.toolName !== "string"
    || !deepagents.toolName
    || deepagents.toolName.trim() !== deepagents.toolName
    || !isJsonObject(deepagents.originalArgs)
  ) throw new ToolReviewContractError("metadata.deepagents 字段无效")

  const allowedDecisions = parseDecisions(deepagents.allowedDecisions, "allowedDecisions")
  const actions = parseNativeRequest(metadata.langgraphValue)
  const actionIndex = deepagents.actionIndex as number
  const action = actions[actionIndex]
  if (!action) throw new ToolReviewContractError("actionIndex 超出原生分组")
  const expectedId = actions.length === 1
    ? deepagents.nativeInterruptId
    : `${deepagents.nativeInterruptId}#${actionIndex}`
  if (
    interrupt.id !== expectedId
    || deepagents.toolName !== action.name
    || !jsonValuesEqual(deepagents.originalArgs, action.args)
    || allowedDecisions.length !== action.allowedDecisions.length
    || allowedDecisions.some((decision, index) => decision !== action.allowedDecisions[index])
  ) throw new ToolReviewContractError("Tool review metadata 与原生 action 不一致")

  return {
    schema: TOOL_REVIEW_SCHEMA,
    nativeInterruptId: deepagents.nativeInterruptId,
    actionIndex,
    toolName: deepagents.toolName,
    allowedDecisions,
    originalArgs: deepagents.originalArgs,
  }
}
