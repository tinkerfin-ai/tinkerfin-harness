import { describe, expect, it } from "vitest"

import type { JsonValue } from "../../../types"
import fixture from "./contracts/tool-review.fixture.json"
import {
  TOOL_REVIEW_SCHEMA,
  ToolReviewContractError,
  parseToolReviewInterrupt,
} from "./toolReviewContract"

const validInterrupt = () => ({
  id: "fixture-native-interrupt",
  reason: "tool_call",
  toolCallId: "tool:cm9vdA:Y2FsbC1maXh0dXJl",
  metadata: {
    langgraphValue: {
      action_requests: [{
        name: "write_file",
        args: fixture.originalArgs,
      }],
      review_configs: [{
        action_name: "write_file",
        allowed_decisions: fixture.allowedDecisions,
      }],
    },
    deepagents: fixture,
  },
})

const withSourceArgs = (sourceArgs: JsonValue) => {
  const value = validInterrupt()
  return {
    ...value,
    metadata: {
      ...value.metadata,
      langgraphValue: {
        ...value.metadata.langgraphValue,
        action_requests: value.metadata.langgraphValue.action_requests.map(action => ({
          ...action,
          source_args: sourceArgs,
        })),
      },
    },
  }
}

describe("Tool review current contract", () => {
  it("parses the generated cross-language fixture", () => {
    expect(parseToolReviewInterrupt(validInterrupt())).toEqual(fixture)
    expect(fixture.schema).toBe(TOOL_REVIEW_SCHEMA)
  })

  it.each([{ sourceArgs: { file_path: "report.txt" } }, { sourceArgs: null }])(
    "保留有效审批参数并校验原始提案 $sourceArgs",
    ({ sourceArgs }) => {
      expect(parseToolReviewInterrupt(withSourceArgs(sourceArgs))).toEqual(fixture)
    },
  )

  it.each([
    { label: "array", sourceArgs: [] },
    { label: "string", sourceArgs: "report.txt" },
    { label: "number", sourceArgs: 1 },
    { label: "boolean", sourceArgs: true },
  ])("拒绝非对象原始提案 $label", ({ sourceArgs }) => {
    expect(() => parseToolReviewInterrupt(withSourceArgs(sourceArgs))).toThrow(ToolReviewContractError)
  })

  it.each([
    ["missing metadata", (value: ReturnType<typeof validInterrupt>) => ({
      ...value,
      metadata: undefined,
    })],
    ["missing schema", (value: ReturnType<typeof validInterrupt>) => {
      const deepagents = Object.fromEntries(
        Object.entries(value.metadata.deepagents).filter(([key]) => key !== "schema"),
      )
      return { ...value, metadata: { ...value.metadata, deepagents } }
    }],
    ["unknown field", (value: ReturnType<typeof validInterrupt>) => ({
      ...value,
      metadata: {
        ...value.metadata,
        deepagents: { ...value.metadata.deepagents, unknown: true },
      },
    })],
    ["unknown native action field", (value: ReturnType<typeof validInterrupt>) => ({
      ...value,
      metadata: {
        ...value.metadata,
        langgraphValue: {
          ...value.metadata.langgraphValue,
          action_requests: value.metadata.langgraphValue.action_requests.map(action => ({
            ...action,
            unknown: true,
          })),
        },
      },
    })],
    ["tampered args", (value: ReturnType<typeof validInterrupt>) => ({
      ...value,
      metadata: {
        ...value.metadata,
        deepagents: {
          ...value.metadata.deepagents,
          originalArgs: { file_path: "/other" },
        },
      },
    })],
    ["tampered action index", (value: ReturnType<typeof validInterrupt>) => ({
      ...value,
      metadata: {
        ...value.metadata,
        deepagents: { ...value.metadata.deepagents, actionIndex: 1 },
      },
    })],
    ["illegal decision", (value: ReturnType<typeof validInterrupt>) => ({
      ...value,
      metadata: {
        ...value.metadata,
        deepagents: { ...value.metadata.deepagents, allowedDecisions: ["allow"] },
      },
    })],
    ["wrong public id", (value: ReturnType<typeof validInterrupt>) => ({
      ...value,
      id: "other",
    })],
  ])("rejects %s", (_name, mutate) => {
    expect(() => parseToolReviewInterrupt(mutate(validInterrupt()))).toThrow(ToolReviewContractError)
  })
})
