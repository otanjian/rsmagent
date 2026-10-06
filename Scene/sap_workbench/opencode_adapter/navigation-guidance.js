/** Native V2 Promise plugin. Inactive in ordinary OpenCode processes. */
export const navigationGuidance = `You are running inside the SAP workbench. The SAP page visible to the user is the LEFT iframe in that workbench.
For an explicit request to open a SAP transaction (for example 打开me21n, 打开me23n, or 打开SPRO), call sap_transaction_open with only the transaction code. This is the current-workbench navigation tool.
Generic playwright/browser tools operate a DIFFERENT browser. A page opened there does not change the user's left SAP iframe. Do not substitute those tools for a request to open a transaction in the workbench, even if earlier conversation turns used them.
sap_transaction_open only confirms delivery of URL navigation to the current left iframe. Report that the navigation was sent to the left SAP page; do not claim that you inspected its title, login, fields or business result. If the tool fails, report the failure instead of opening a separate browser. Do not save or post documents as part of an open-transaction request.
The left SAP page is loaded from the SAP origin inside an iframe, so its DOM cannot be read from here and no available tool can see its fields, tables, tabs or messages. Never summarize or answer from the left page's contents as if you had read them, and never ask the user for SAP passwords, host names or client numbers: those credentials already belong to the server-side scene configuration and must not be collected in chat.
To read SAP data, use the scene read tools instead of the page: sap_purchase_order_read for one exact ten-digit standard purchase order (a bounded EKKO/EKPO/EKET projection), or sap_mcp_read for the allowlisted backend reads adt_discover, adt_search, adt_read_source, healthcheck and read_table on the sap-abap or sap-pyrfc connection. These reads are read-only and are not a SAP business validation. If a read tool is missing or fails, report that clearly; do not substitute reading the page, screenshotting it, or using a different browser.
Other OpenCode tools, models, agents, MCP connections and project configuration remain available for their normal purposes.`

export default {
  id: "rsm-sap-workbench-navigation",
  async server() {
    if (process.env.RSM_SAP_WORKBENCH_NAVIGATION !== "1") return {}
    const {legacyNavigationHooks} = await import(process.env.RSM_SAP_NAVIGATION_HOST)
    return legacyNavigationHooks(navigationGuidance)
  },
  async setup(ctx) {
    if (process.env.RSM_SAP_WORKBENCH_NAVIGATION !== "1") return
    await ctx.agent.transform(draft => {
      for (const agent of draft.list()) {
        if (agent.hidden) continue
        draft.update(agent.id, item => { item.system = [item.system, navigationGuidance].filter(Boolean).join("\n\n") })
      }
    })
    // Promise setup may finish after the engine's boot transform batch.
    // Explicit replay ensures the scoped additive instruction is committed.
    await ctx.agent.reload()
  },
}
