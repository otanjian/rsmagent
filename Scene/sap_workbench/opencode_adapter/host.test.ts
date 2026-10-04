import { expect, test } from "bun:test"
import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { execFileSync } from "node:child_process"
import { loadRuntime, createHost } from "./host"
import { createTools, refusalMessage } from "./tools"
import { sceneContext } from "./context"

const root = process.env.SAP_OPENCODE_ROOT

test("tool failures explain known page states without reflecting arbitrary upstream content", () => {
  expect(refusalMessage("field_value_rejected")).toContain("not confirmed");
  expect(refusalMessage("navigation_not_observed")).toContain("do not repeat");
  expect(refusalMessage("sap_login_required")).toContain("Do not request a password");
  for (const code of ["secret=password", "__proto__", {password: "private"}, null]) {
    expect(refusalMessage(code)).toBe(refusalMessage(undefined));
  }
})

test.skipIf(!root)("scene host shares canonical tools with the real HTTP Session V2 runner", async () => {
  const dir = await mkdtemp(join(tmpdir(), "sap-opencode-host-"))
  const config = join(dir, "config")
  const project = join(dir, "project")
  await mkdir(config)
  await mkdir(project)
  // Isolate all OpenCode state. This test never opens SAP or an external model.
  process.env.OPENCODE_DB = join(dir, "opencode.db")
  process.env.OPENCODE_TEST_HOME = dir
  process.env.OPENCODE_CONFIG_DIR = config
  process.env.XDG_DATA_HOME = join(dir, "data")
  process.env.XDG_CONFIG_HOME = config
  process.env.XDG_STATE_HOME = join(dir, "state")
  process.env.XDG_CACHE_HOME = join(dir, "cache")
  process.env.OPENCODE_DISABLE_MODELS_FETCH = "1"
  process.env.OPENCODE_DISABLE_AUTOUPDATE = "1"
  delete process.env.OPENCODE_SERVER_PASSWORD
  delete process.env.OPENCODE_CONFIG_CONTENT
  const seen: string[] = []
  let turn = 0
  let slow = false
  const calls: Array<{service_id: string; session_id: string; message_id: string; call_id: string; input: unknown}> = []
  const cancellations: typeof calls = []
  let finishSlow: ((response: Response) => void) | undefined
  const bridge = Bun.serve({hostname: "127.0.0.1", port: 0, async fetch(request) {
    expect(request.headers.get("authorization")).toBe("Bearer test-bridge-token")
    const body = await request.json()
    if (new URL(request.url).pathname === "/cancel") {
      cancellations.push(body)
      finishSlow?.(Response.json({output: "cancelled"}))
      return Response.json({ok: true})
    }
    calls.push(body)
    if (slow) return new Promise<Response>(resolve => {finishSlow = resolve})
    return Response.json({output: "adapter ready"})
  }})
  const provider = Bun.serve({ hostname: "127.0.0.1", port: 0, async fetch(request) {
    const body = await request.json()
    seen.push(...(body.tools ?? []).map((tool: {function: {name: string}}) => tool.function.name))
    turn++
    const delta = turn === 1 || slow
      ? { role: "assistant", tool_calls: [{index: 0, id: slow ? "call_cancel" : "call_probe", type: "function", function: {name: "sap_page_read", arguments: '{"session_id":"forged-session"}'}}] }
      : {role: "assistant", content: "Probe completed."}
    return new Response([
      {id: "probe", object: "chat.completion.chunk", model: "probe", choices: [{index: 0, delta, finish_reason: null}]},
      {id: "probe", object: "chat.completion.chunk", model: "probe", choices: [{index: 0, delta: {}, finish_reason: turn === 1 || slow ? "tool_calls" : "stop"}], usage: {prompt_tokens: 1, completion_tokens: 1, total_tokens: 2}},
    ].map(part => `data: ${JSON.stringify(part)}\n\n`).join("") + "data: [DONE]\n\n", {headers: {"Content-Type": "text/event-stream"}})
  }})
  await writeFile(join(config, "opencode.json"), JSON.stringify({
    compaction: {auto: sceneContext.compaction.auto, buffer: sceneContext.compaction.reserved,
      keep: {tokens: sceneContext.compaction.preserve_recent_tokens}},
    providers: {probe: {api: {type: "aisdk", package: "@ai-sdk/openai-compatible", url: `${provider.url}v1`, settings: {apiKey: "local-test"}}, models: {probe: {limit: sceneContext.limit}}}},
    agents: {sap: {system: "Use only sap_page_read.", permissions: [{action: "*", resource: "*", effect: "deny"}, {action: "sap_page_read", resource: "*", effect: "allow"}]}},
  }))
  const runtime = await loadRuntime(root!)
  const { Schema } = runtime
  const { Config } = await import(join(root!, "packages/core/src/config.ts"))
  const { ConfigMigrateV1 } = await import(join(root!, "packages/core/src/v1/config/migrate.ts"))
  const migrated = Schema.decodeUnknownSync(Config.Info)(ConfigMigrateV1.migrate({compaction: sceneContext.compaction}))
  expect(migrated.compaction?.buffer).toBe(sceneContext.compaction.reserved)
  expect(sceneContext.limit.context - Math.max(sceneContext.limit.output, migrated.compaction!.buffer!)).toBeGreaterThan(8000)
  Schema.decodeUnknownSync(Config.Info)(JSON.parse(await Bun.file(join(config, "opencode.json")).text()))
  // A project file must not be able to loosen the deployment permission set.
  await writeFile(join(project, "opencode.json"), JSON.stringify({agents: {sap: {permissions: [{action: "*", resource: "*", effect: "allow"}]}}}))
  const tools = createTools(runtime, {
    url: bridge.url.toString(), token: "test-bridge-token", serviceID: "service-one",
  })
  const host = await createHost(runtime, tools, JSON.parse(await Bun.file(join(config, "opencode.json")).text()))
  const api = async (path: string, body?: unknown) => {
    const response = await host.fetch(new Request(`http://localhost${path}`, body === undefined ? undefined : {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
    }))
    const text = await response.text()
    expect(response.status, text).toBeLessThan(300)
    return text ? JSON.parse(text) : undefined
  }
  const eventAbort = new AbortController()
  const events: Array<{type: string; location?: {directory: string}}> = []
  let readEvents: Promise<void> | undefined
  let eventReader: ReadableStreamDefaultReader<Uint8Array> | undefined
  try {
    // Core Tool values hide their schemas. Check the public definition/settle
    // contract: invalid navigation inputs must fail before reaching the bridge.
    const navigation = runtime.Tool.definition("sap_page_navigate", tools.sap_page_navigate)
    expect(navigation.inputSchema.required).toContain("revision")
    expect(navigation.inputSchema.properties.revision.type).toBe("string")
    for (const input of [{transaction: "SPRO"}, {revision: 42, transaction: "SPRO"}]) {
      const validation = await runtime.Effect.runPromiseExit(runtime.Tool.settle(tools.sap_page_navigate, {
        type: "tool-call", id: "call_schema_probe", name: "sap_page_navigate", input,
      }, {sessionID: "ses_schema_probe", agent: "sap", assistantMessageID: "msg_schema_probe", toolCallID: "call_schema_probe"}))
      expect(runtime.Exit.isFailure(validation)).toBe(true)
      expect(calls).toHaveLength(0)
    }
    expect((await api("/api/health")).healthy).toBe(true)
    const stream = await host.fetch(new Request("http://localhost/api/event", {signal: eventAbort.signal}))
    readEvents = (async () => {
      let pending = ""
      const reader = eventReader = stream.body!.getReader()
      try {
        while (true) {
          const chunk = await reader.read()
          if (chunk.done) break
          pending += new TextDecoder().decode(chunk.value)
          let end
          while ((end = pending.indexOf("\n\n")) !== -1) {
            const line = pending.slice(0, end).split("\n").find(line => line.startsWith("data:"))
            pending = pending.slice(end + 2)
            if (line) events.push(JSON.parse(line.slice(5)))
          }
        }
      } catch (error) { if (!eventAbort.signal.aborted) throw error }
      finally { await reader.cancel().catch(() => {}) }
    })()
    const session = (await api("/api/session", {id: `ses_sap_probe_${crypto.randomUUID()}`, agent: "sap", location: {directory: project}, model: {providerID: "probe", id: "probe"}})).data
    // Location producers initialize asynchronously in this pinned build.
    // A prompt must not race their permission registration.
    const locationQuery = `?location[directory]=${encodeURIComponent(project)}`
    const readyDeadline = Date.now() + 5000
    let agents
    do {
      agents = await api(`/api/agent${locationQuery}`)
      if (agents.data.some((agent: {id: string}) => agent.id === "sap")) break
      await Bun.sleep(25)
    } while (Date.now() < readyDeadline)
    expect(agents.data.some((agent: {id: string}) => agent.id === "sap")).toBe(true)
    await api(`/api/session/${session.id}/prompt`, {prompt: {text: "Call sap_page_read once."}})
    const deadline = Date.now() + 15000
    while (!calls.length && Date.now() < deadline) await Bun.sleep(50)
    const context = await api(`/api/session/${session.id}/context`)
    expect(calls, JSON.stringify(context)).toHaveLength(1)
    expect(calls[0].session_id).toBe(session.id)
    expect(calls[0].service_id).toBe("service-one")
    expect(calls[0].call_id).toBe("call_probe")
    expect(calls[0].input).toEqual({})
    expect(seen).toContain("sap_page_read")
    expect(seen).not.toContain("bash")
    expect(seen).not.toContain("sap_page_fill")
    // Wait for durable completion, not just the HTTP request reaching the tool.
    const settleDeadline = Date.now() + 5000
    while (Object.keys((await api("/api/session/active")).data).length && Date.now() < settleDeadline) await Bun.sleep(25)
    expect((await api("/api/session/active")).data).toEqual({})
    expect(events.some(event => event.type === "session.next.compaction.started")).toBe(false)
    expect(events.some(event => event.type === "session.next.tool.success")).toBe(true)
    expect(events.filter(event => event.type.startsWith("session.")).every(event => event.location?.directory === project), JSON.stringify(events.map(event => ({type: event.type, location: event.location})))).toBe(true)
    // Feed real backend events through the shipped scene adapter and the
    // untouched native Web reducer. Detect wire/build drift before deployment.
    const repo = join(import.meta.dir, "../../..")
    const projected = JSON.parse(execFileSync(join(repo, ".venv/bin/python"), ["-c",
      "import json,sys; from Scene.sap_workbench.backend.native_events import NativeEvents; adapt=NativeEvents(); print(json.dumps([item for event in json.load(sys.stdin) for item in adapt(event)]))",
    ], {cwd: repo, input: JSON.stringify(events), encoding: "utf8"}))
    const { createV2SessionReducer } = await runtime.source("packages/app/src/context/server-session-v2-reducer.ts")
    const reducer = createV2SessionReducer()
    let messages = []
    for (const event of projected) messages = reducer.reduce(messages, event)?.messages ?? messages
    expect(messages.some(message => message.type === "user" && message.text === "Call sap_page_read once.")).toBe(true)
    expect(messages.some(message => message.type === "assistant" && message.time.completed && message.content.some(part => part.type === "text" && part.text === "Probe completed."))).toBe(true)
    expect(messages.some(message => message.type === "assistant" && message.content.some(part => part.type === "tool" && part.state.status === "completed"))).toBe(true)
    expect(projected.some(event => event.type === "session.execution.succeeded")).toBe(true)
    slow = true
    await api(`/api/session/${session.id}/prompt`, {prompt: {text: "Call sap_page_read and wait."}})
    const cancelDeadline = Date.now() + 5000
    while (calls.length < 2 && Date.now() < cancelDeadline) await Bun.sleep(25)
    expect(calls).toHaveLength(2)
    await api(`/api/session/${session.id}/interrupt`, {})
    while (!cancellations.length && Date.now() < cancelDeadline) await Bun.sleep(25)
    expect(cancellations).toHaveLength(1)
    expect(cancellations[0].session_id).toBe(session.id)
    expect(cancellations[0].call_id).toBe("call_cancel")
  } finally {
    eventAbort.abort()
    await eventReader?.cancel()
    await readEvents
    await host.close()
    provider.stop(true)
    bridge.stop(true)
    await rm(dir, {recursive: true, force: true})
  }
}, 40000)
