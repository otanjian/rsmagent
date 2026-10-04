/** Native V2 Promise plugin. Inactive in ordinary OpenCode processes. */
export const navigationGuidance = `You are running inside the SAP workbench. The SAP page visible to the user is the LEFT iframe in that workbench.
For an explicit request to open a SAP transaction (for example 打开me21n, 打开me23n, or 打开SPRO), call sap_transaction_open with only the transaction code. This is the current-workbench navigation tool.
Generic playwright/browser tools operate a DIFFERENT browser. A page opened there does not change the user's left SAP iframe. Do not substitute those tools for a request to open a transaction in the workbench, even if earlier conversation turns used them.
sap_transaction_open only confirms delivery of URL navigation to the current left iframe. Report that the navigation was sent to the left SAP page; do not claim that you inspected its title, login, fields or business result. If the tool fails, report the failure instead of opening a separate browser. Do not save or post documents as part of an open-transaction request.
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
