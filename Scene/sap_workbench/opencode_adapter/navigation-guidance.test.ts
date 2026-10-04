import {test, expect} from "bun:test"
import plugin, {navigationGuidance} from "./navigation-guidance.js"

test("ordinary OpenCode processes receive no workbench instruction", async () => {
  const original = process.env.RSM_SAP_WORKBENCH_NAVIGATION
  delete process.env.RSM_SAP_WORKBENCH_NAVIGATION
  try {
    await plugin.setup({agent: {transform() {throw new Error("unexpected transform")}, reload() {throw new Error("unexpected reload")}}})
  } finally {
    if (original === undefined) delete process.env.RSM_SAP_WORKBENCH_NAVIGATION
    else process.env.RSM_SAP_WORKBENCH_NAVIGATION = original
  }
})

test("workbench guidance preserves project instructions and settings", async () => {
  const original = process.env.RSM_SAP_WORKBENCH_NAVIGATION
  process.env.RSM_SAP_WORKBENCH_NAVIGATION = "1"
  const settings = {permissions: [{action: "read", effect: "allow"}], model: "project/model"}
  const items = [{id: "build", system: "Original instruction", ...settings}, {id: "hidden", hidden: true, system: "Private"}]
  let reloaded = false
  try {
    await plugin.setup({agent: {
      async transform(update) { update({list: () => items, update: (id, change) => change(items.find(item => item.id === id))}) },
      async reload() { reloaded = true },
    }})
    expect(items[0].system).toBe("Original instruction\n\n" + navigationGuidance)
    expect(items[0].permissions).toBe(settings.permissions)
    expect(items[0].model).toBe("project/model")
    expect(items[1].system).toBe("Private")
    expect(reloaded).toBe(true)
  } finally {
    if (original === undefined) delete process.env.RSM_SAP_WORKBENCH_NAVIGATION
    else process.env.RSM_SAP_WORKBENCH_NAVIGATION = original
  }
})
