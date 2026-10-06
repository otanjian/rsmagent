import {test, expect} from "bun:test"
import {mkdtemp, mkdir, writeFile, rm} from "node:fs/promises"
import {tmpdir} from "node:os"
import {join} from "node:path"
import {createNativeHost, legacyNavigationHooks} from "./native-host"
import {createTools} from "./tools"
import {navigationGuidance} from "./navigation-guidance.js"

const root = process.env.SAP_OPENCODE_ROOT
test.skipIf(!root)("native listener exposes the bound transaction tool alongside native tools and project models", async () => {
  const dir = await mkdtemp(join(tmpdir(), "sap-native-navigation-")), project = join(dir, "project"), config = join(dir, ".config/opencode")
  await mkdir(config, {recursive: true}); await mkdir(project)
  process.env.OPENCODE_DB = join(dir, "test.db")
  process.env.OPENCODE_TEST_HOME = dir
  process.env.XDG_CONFIG_HOME = join(dir, ".config")
  process.env.XDG_DATA_HOME = join(dir, "data")
  process.env.XDG_STATE_HOME = join(dir, "state")
  process.env.XDG_CACHE_HOME = join(dir, "cache")
  process.env.OPENCODE_DISABLE_MODELS_FETCH = "1"
  process.env.OPENCODE_DISABLE_AUTOUPDATE = "1"
  delete process.env.OPENCODE_SERVER_PASSWORD
  delete process.env.OPENCODE_CONFIG_CONTENT
  const calls: any[] = [], seen: string[] = [], systems: string[] = []
  let turn = 0
  const bridge = Bun.serve({hostname: "127.0.0.1", port: 0, async fetch(request) {
    expect(request.headers.get("Authorization")).toBe("Bearer test-token")
    calls.push(await request.json())
    return Response.json({output: JSON.stringify({status: "navigation_applied", sap_page_verified: false})})
  }})
  const provider = Bun.serve({hostname: "127.0.0.1", port: 0, async fetch(request) {
    const body = await request.json(); seen.push(...(body.tools ?? []).map((t: any) => t.function.name))
    systems.push(...body.messages.filter((m: any) => m.role === "system").map((m: any) => String(m.content)))
    const hasToolResult = body.messages.some((m: any) => m.role === "tool")
    const toolTurn = body.tools?.some((t: any) => t.function.name === "sap_transaction_open") && !hasToolResult
    ++turn
    const delta = toolTurn
      ? {role: "assistant", tool_calls: [{index: 0, id: `call_bound_${turn}`, type: "function", function: {name: "sap_transaction_open", arguments: '{"transaction":"ME21N"}'}}]}
      : {role: "assistant", content: "Navigation applied."}
    return new Response([
      {id: "test", object: "chat.completion.chunk", model: "probe", choices: [{index: 0, delta, finish_reason: null}]},
      {id: "test", object: "chat.completion.chunk", model: "probe", choices: [{index: 0, delta: {}, finish_reason: toolTurn ? "tool_calls" : "stop"}], usage: {prompt_tokens: 1, completion_tokens: 1, total_tokens: 2}},
    ].map(p => `data: ${JSON.stringify(p)}\n\n`).join("") + "data: [DONE]\n\n", {headers: {"Content-Type": "text/event-stream"}})
  }})
  await writeFile(join(project, "opencode.json"), JSON.stringify({
    provider: {probe: {npm: "@ai-sdk/openai-compatible", options: {baseURL: `${provider.url}v1`, apiKey: "local-test"},
      models: {probe: {limit: {context: 32000, output: 4096}}, another: {limit: {context: 32000, output: 4096}}}}},
    model: "probe/probe", small_model: "probe/probe",
    agent: {build: {prompt: "Keep my original project instructions."}},
    permission: {"*": "allow"},
  }))
  const host = await createNativeHost(root!, runtime => createTools(runtime, {url: String(bridge.url), token: "test-token", serviceID: "service-test"}, {nativeNavigation: true}), project)
  const api = async (path: string, body?: unknown) => {
    const response = await fetch(`http://127.0.0.1:${host.port}${path}`, {signal: AbortSignal.timeout(10000), ...(body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})})
    const text = await response.text(); expect(response.status, text).toBeLessThan(300)
    return text ? JSON.parse(text) : undefined
  }
  try {
    expect((await api("/api/health")).healthy).toBe(true)
    const query = `?location[directory]=${encodeURIComponent(project)}`
    const deadline = Date.now() + 10000
    let agents
    do {agents = await api("/api/agent" + query); if (agents.data.some((a: any) => a.id === "build" && a.system?.includes("sap_transaction_open"))) break; await Bun.sleep(25)} while (Date.now() < deadline)
    expect(agents.data.some((a: any) => a.id === "build")).toBe(true)
    const models = (await api("/api/model" + query)).data.filter((m: any) => m.providerID === "probe")
    expect(models.map((m: any) => m.id).sort()).toEqual(["another", "probe"])
    const session = (await api("/api/session", {id: `ses_rsm_test_${crypto.randomUUID()}`, agent: "build", location: {directory: project}, model: {providerID: "probe", id: "probe"}})).data
    await api(`/api/session/${session.id}/prompt`, {prompt: {text: "打开me21n"}})
    const finish = Date.now() + 12000
    while (!calls.length && Date.now() < finish) await Bun.sleep(25)
    expect(calls).toHaveLength(1)
    expect(calls[0]).toMatchObject({service_id: "service-test", session_id: session.id, call_id: "call_bound_1", action: "transaction_open", input: {transaction: "ME21N"}})
    expect(seen).toContain("sap_transaction_open")
    expect(seen).toContain("bash")
    expect(seen).toContain("read")
    // The left SAP page is a cross-origin iframe, so the configured read-only MCP
    // tools are the model's only way to read SAP data and must stay registered.
    expect(seen).toContain("sap_mcp_read")
    expect(seen).toContain("sap_purchase_order_read")
    // Page tools have no bound page in this display mode and must not appear.
    expect(seen).not.toContain("sap_page_read")
    expect(seen).not.toContain("sap_page_fill")
    expect(systems.join("\n")).toContain("sap_transaction_open")
    expect(systems.join("\n")).toContain("Keep my original project instructions.")
    // Exercise the V1 plugin entry directly; its cold CLI config loader may
    // install dependencies. Real Web/V1 runner verification is done in Chrome.
    const hooks = await legacyNavigationHooks(navigationGuidance)
    const original = {system: ["Keep native instructions"]}
    await hooks["experimental.chat.system.transform"]({}, original)
    expect(original.system).toEqual(["Keep native instructions", navigationGuidance])
    let asked = false
    const output = await hooks.tool.sap_transaction_open.execute({transaction: "ME23N"}, {
      sessionID: session.id, messageID: "message-v1", callID: "call-v1", agent: "build", abort: new AbortController().signal,
      async ask(input: any) {expect(input.permission).toBe("sap_transaction_open"); asked = true},
    })
    expect(JSON.parse(output).status).toBe("navigation_applied")
    expect(asked).toBe(true)
    expect(calls).toHaveLength(2)
    expect(calls[1]).toMatchObject({service_id: "service-test", session_id: session.id, message_id: "message-v1", call_id: "call-v1", action: "transaction_open", input: {transaction: "ME23N"}})
    // A native V1 UI builds its tool list from these hooks, so the same read
    // tools must be reachable there and keep their own permission prompt.
    expect(Object.keys(hooks.tool).sort()).toEqual(["sap_mcp_read", "sap_purchase_order_read", "sap_transaction_open"])
    let readAsked: any
    const readOutput = await hooks.tool.sap_mcp_read.execute({connection: "sap-pyrfc", tool: "healthcheck", arguments: {}}, {
      sessionID: session.id, messageID: "message-v1-read", callID: "call-v1-read", agent: "build", abort: new AbortController().signal,
      async ask(input: any) {readAsked = input},
    })
    expect(readAsked.permission).toBe("sap_mcp_read")
    expect(JSON.parse(readOutput).status).toBe("navigation_applied")
    expect(calls).toHaveLength(3)
    expect(calls[2]).toMatchObject({service_id: "service-test", session_id: session.id, message_id: "message-v1-read", call_id: "call-v1-read",
      action: "mcp_read", input: {connection: "sap-pyrfc", tool: "healthcheck", arguments: {}}})
  } finally {
    await host.stop(true); bridge.stop(true); provider.stop(true)
    await rm(dir, {recursive: true, force: true})
  }
}, 35000)
