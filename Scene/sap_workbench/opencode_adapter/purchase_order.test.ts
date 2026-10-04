import {expect, test} from "bun:test"
import {loadRuntime} from "./host"
import {createTools, refusalMessage} from "./tools"

const root = process.env.SAP_OPENCODE_ROOT

test.skipIf(!root)("canonical PO tool only forwards its document number and runner identity", async () => {
  const runtime = await loadRuntime(root!)
  const calls: unknown[] = []
  const output = JSON.stringify({scope: "standard_material_purchase_order_v1", document: {document_number: "4500000123"},
    business_validated: false, complete_business_document: false, submission_authority: false})
  const bridge = Bun.serve({hostname: "127.0.0.1", port: 0, async fetch(request) {
    expect(new URL(request.url).pathname).toBe("/call")
    expect(request.headers.get("authorization")).toBe("Bearer test-po-bridge")
    calls.push(await request.json())
    return Response.json({output})
  }})
  try {
    const tools = createTools(runtime, {url: bridge.url.toString(), token: "test-po-bridge", serviceID: "service-po"})
    const definition = runtime.Tool.definition("sap_purchase_order_read", tools.sap_purchase_order_read)
    expect(definition.inputSchema.required).toEqual(["document_number"])
    expect(Object.keys(definition.inputSchema.properties)).toEqual(["document_number"])
    expect(definition.description).toContain("not a transaction snapshot")
    const context = {sessionID: "ses_owner_po", agent: "sap", assistantMessageID: "msg_owner_po", toolCallID: "call_owner_po"}
    for (const input of [{}, {document_number: 4500000123}]) {
      const result = await runtime.Effect.runPromiseExit(runtime.Tool.settle(tools.sap_purchase_order_read, {
        type: "tool-call", id: context.toolCallID, name: "sap_purchase_order_read", input,
      }, context))
      expect(runtime.Exit.isFailure(result)).toBe(true)
      expect(calls).toHaveLength(0)
    }
    const result = await runtime.Effect.runPromise(runtime.Tool.settle(tools.sap_purchase_order_read, {
      type: "tool-call", id: context.toolCallID, name: "sap_purchase_order_read",
      input: {document_number: "4500000123", client: "300", connection_id: "forged", session_id: "forged"},
    }, context))
    expect(JSON.stringify(result)).toContain(output.replaceAll('"', '\\"'))
    expect(calls).toEqual([{service_id: "service-po", session_id: context.sessionID, message_id: context.assistantMessageID,
      call_id: context.toolCallID, action: "purchase_order_read", input: {document_number: "4500000123"}}])
    expect(refusalMessage("purchase_order_not_found")).toContain("does not prove")
    expect(refusalMessage("purchase_order_result_too_large")).toContain("no truncated")
  } finally {
    bridge.stop(true)
  }
})
