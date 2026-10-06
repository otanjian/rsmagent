/** The iframe workbench shows a cross-origin SAP page it cannot read as DOM, so
 * the configured read-only MCP tools are the model's only way to read SAP data
 * and must be registered in both display modes. Page tools stay unregistered
 * wherever no page is bound. */
import {expect, test} from "bun:test"
import {loadRuntime} from "./host"
import {createTools} from "./tools"

const root = process.env.SAP_OPENCODE_ROOT

test.skipIf(!root)("workbench tool sets follow the display mode's real capabilities", async () => {
  const runtime = await loadRuntime(root!)
  const bridge = Bun.serve({hostname: "127.0.0.1", port: 0, fetch: () => Response.json({output: "{}"})})
  const bridgeOptions = {url: bridge.url.toString(), token: "test-token", serviceID: "service-tools"}
  try {
    const native = createTools(runtime, bridgeOptions, {nativeNavigation: true})
    expect(Object.keys(native).sort()).toEqual(["sap_mcp_read", "sap_purchase_order_read", "sap_transaction_open"])
    const managed = createTools(runtime, bridgeOptions)
    expect(Object.keys(managed).sort()).toEqual(["sap_mcp_read", "sap_page_fill", "sap_page_interact",
      "sap_page_navigate", "sap_page_read", "sap_page_scroll", "sap_purchase_order_read"])
    // Both modes share one read surface: allowlisted tools, no identity fields.
    const nativeRead = runtime.Tool.definition("sap_mcp_read", native.sap_mcp_read)
    expect(nativeRead.inputSchema.required.sort()).toEqual(["arguments", "connection", "tool"])
    expect(nativeRead.description).toContain("adt_discover")
    expect(nativeRead.description).toContain("Page actions must use the page tools")
    expect(runtime.Tool.definition("sap_mcp_read", managed.sap_mcp_read).description).toBe(nativeRead.description)
    // Navigation states the boundary that the page cannot be read as DOM.
    expect(runtime.Tool.definition("sap_transaction_open", native.sap_transaction_open).description)
      .toContain("cannot be read as DOM")
  } finally {
    bridge.stop(true)
  }
})
