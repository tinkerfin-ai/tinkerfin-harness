import { describe, expect, it } from "vitest"

import fixture from "./contracts/subagent-provenance.fixture.json"
import {
  SUBAGENT_PROVENANCE_SCHEMA,
  SubagentProvenanceContractError,
  parseSubagentProvenance,
} from "./subagentProvenanceContract"

describe("Subagent provenance current contract", () => {
  it("parses the generated cross-language fixture", () => {
    expect(parseSubagentProvenance(fixture)).toEqual(fixture)
    expect(fixture.schema).toBe(SUBAGENT_PROVENANCE_SCHEMA)
  })

  it("preserves the complete task description", () => {
    const value = { ...fixture, description: "  分析门店\n保留原始任务  " }
    expect(parseSubagentProvenance(value).description).toBe(value.description)
  })

  it("accepts the framework's UTF-8 parent tool identity", () => {
    const value = {
      ...fixture,
      parentGraphNamespace: ["tools:研究"],
      parentToolCallId: "tf:tool:W1sidG9vbHM656CU56m2Il0sIuWnlOa0viJd",
    }
    expect(parseSubagentProvenance(value)).toEqual(value)
  })

  it.each([
    ["missing schema", (() => {
      return Object.fromEntries(
        Object.entries(fixture).filter(([key]) => key !== "schema"),
      )
    })()],
    ["unknown field", { ...fixture, unknown: true }],
    ["physical namespace", { ...fixture, graphNamespace: ["tools:other"] }],
    ["physical graph task", { ...fixture, graphTaskId: "other" }],
    ["invalid parent namespace", { ...fixture, parentGraphNamespace: [" "] }],
    ["missing parent tool", { ...fixture, parentToolCallId: null }],
    ["unscoped parent tool", { ...fixture, parentToolCallId: "not-scoped" }],
    ["conflicting parent scope", { ...fixture, parentGraphNamespace: ["tools:different"] }],
    ["parent message instead of tool", { ...fixture, parentToolCallId: fixture.parentToolCallId.replace("tf:tool:", "tf:message:") }],
    ["malformed parent encoding", { ...fixture, parentToolCallId: "tf:tool:--__" }],
    ["invalid invocation ID", { ...fixture, subagentInvocationId: "subagent-invalid" }],
    ["empty request run", { ...fixture, requestRunId: "" }],
  ])("rejects %s", (_name, value) => {
    expect(() => parseSubagentProvenance(value)).toThrow(SubagentProvenanceContractError)
  })
})
