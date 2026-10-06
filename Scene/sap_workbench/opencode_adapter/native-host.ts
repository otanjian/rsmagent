/** Native OpenCode routes + an additive scene tool, sharing one registry.
 * Uses the installed engine's HTTP/WebSocket layers and configuration discovery.
 */
import { createServer } from "node:http"
import { mkdir, readFile, writeFile } from "node:fs/promises"
import { join } from "node:path"
import { createHash } from "node:crypto"
import { loadRuntime } from "./host"

let navigationRuntime: Awaited<ReturnType<typeof loadRuntime>> | undefined
let navigationTools: Record<string, any> = {}

/** The V1 server hook needs its own zod argument surface and native permission
 * prompt per tool; both follow the tool's own trusted input. Only tools this
 * display mode actually registered are exposed, and every exposed name must
 * appear here. Page tools stay unregistered in the iframe workbench, so they
 * intentionally have no V1 entry. */
const v1ToolSpecs: Record<string, (z: any) => {args: Record<string, unknown>; ask: (input: any) => Record<string, unknown>}> = {
  sap_transaction_open: z => ({
    args: {transaction: z.string()},
    ask: input => ({permission: "sap_transaction_open", patterns: [input.transaction], always: ["*"], metadata: {transaction: input.transaction}}),
  }),
  sap_purchase_order_read: z => ({
    args: {document_number: z.string()},
    ask: input => ({permission: "sap_purchase_order_read", patterns: [input.document_number], always: ["*"], metadata: {document_number: input.document_number}}),
  }),
  sap_mcp_read: z => ({
    args: {connection: z.string(), tool: z.string(), arguments: z.record(z.string(), z.unknown())},
    ask: input => ({permission: "sap_mcp_read", patterns: [input.connection, input.tool], always: ["*"], metadata: {connection: input.connection, tool: input.tool}}),
  }),
}

export async function legacyNavigationHooks(guidance: string) {
  if (!navigationRuntime || !Object.keys(navigationTools).length) throw new Error("Workbench tools are unavailable")
  const {Effect, Tool} = navigationRuntime, {z} = await navigationRuntime.dependency("zod")
  const tool: Record<string, unknown> = {}
  for (const [name, spec] of Object.entries(v1ToolSpecs)) {
    const registered = navigationTools[name]
    if (!registered) continue
    const {args, ask} = spec(z)
    tool[name] = {
      description: Tool.definition(name, registered).description,
      args,
      async execute(input: Record<string, unknown>, context: any) {
        if (!context.sessionID || !context.messageID || !context.callID) throw new Error("Missing trusted tool context")
        await context.ask(ask(input))
        const result = await Effect.runPromise(Tool.settle(registered,
          {id: context.callID, name, input},
          {sessionID: context.sessionID, agent: context.agent, assistantMessageID: context.messageID, toolCallID: context.callID}), {signal: context.abort})
        if (typeof result.structured !== "string") throw new Error("Invalid tool response")
        return result.structured
      },
    }
  }
  return {
    "experimental.chat.system.transform": async (_input: unknown, output: {system: string[]}) => {output.system.push(guidance)},
    tool,
  }
}

/** Normalised comparison for an installed plugin. A Windows checkout stores the
 * same revision with CRLF, and hashing raw bytes would make an identical
 * revision look foreign and block workbench startup. */
export const normaliseGuidance = (text: string) => (text.charCodeAt(0) === 0xfeff ? text.slice(1) : text).replace(/\r\n?/g, "\n")
export const guidanceDigest = (text: string) => createHash("sha256").update(normaliseGuidance(text)).digest("hex")

/** Digests of previously shipped guidance revisions, normalised. Every change to
 * the plugin content adds the digest of the revision it replaces, so a deployed
 * install upgrades in place instead of blocking the workbench host. Keep the
 * oldest entries: a lane that never upgraded must still be able to catch up. */
export const upgradeableGuidance = new Set([
  "a4d3587833ee220f3f64e9271acccc386ecbe6a03774adbc9931c2f19635af32", // first revision installed by this change
  "a40f3755d6975426f9b7f424628d712f25e98b23dae64e09923bda0df93a7bab", // revision replaced by the read-tool guidance
  // The standard-service plugin that replaces this host writes the same file name
  // (`Scene/sap_workbench/project/plugins/rsm-sap-workbench-navigation.js`). It is
  // registered here so deploy order stops being a correctness requirement: whether
  // the new plugin lands before or after this host is retired, this host recognises
  // the file instead of reading it as foreign and refusing to start. Both sides
  // therefore accept the other's revision; each retires with the other's knowledge.
  "358a9018a753ecd72d8c53240fc1b2f78370f64e68e7e594e598322a66e548f8", // standard-service plugin, before it asked the permission layer
  "647c3bbef423bd19aaba6ea0a5bb837c380ee645a402768017d18928efffd206", // standard-service plugin, current revision
])

export async function installNavigationGuidance(directory: string) {
  const content = await readFile(new URL("./navigation-guidance.js", import.meta.url), "utf8")
  const plugins = join(directory, ".opencode", "plugins"), target = join(plugins, "rsm-sap-workbench-navigation.js")
  await mkdir(plugins, {recursive: true})
  try { await writeFile(target, content, {flag: "wx"}) }
  catch (error: any) {
    if (error.code !== "EEXIST") throw error
    const previous = await readFile(target, "utf8")
    // Already this revision: only the checkout's line endings differ.
    if (normaliseGuidance(previous) !== normaliseGuidance(content)) {
      // Upgrade a revision this change shipped; never replace a user's
      // different file with the same plugin name.
      if (!upgradeableGuidance.has(guidanceDigest(previous))) throw error
      await writeFile(target, content)
    }
  }
  process.env.RSM_SAP_WORKBENCH_NAVIGATION = "1"
  process.env.RSM_SAP_NAVIGATION_HOST = import.meta.url
}

export async function createNativeHost(root: string, makeTools: (runtime: Awaited<ReturnType<typeof loadRuntime>>) => Record<string, unknown>, project?: string) {
  if (project) await installNavigationGuidance(project)
  const runtime = await loadRuntime(root)
  const {Effect, Context, Layer, Scope, Exit, HttpRouter, HttpServer, AppNodeBuilder, ApplicationTools, HttpApiApp} = runtime
  const {ConfigProvider} = await runtime.dependency("effect")
  const {NodeHttpServer} = await runtime.dependency("@effect/platform-node")
  const {disposeMiddleware} = await runtime.source("packages/opencode/src/server/routes/instance/httpapi/lifecycle.ts")
  const {WebSocketTracker} = await runtime.source("packages/opencode/src/server/routes/instance/httpapi/websocket-tracker.ts")
  const memoMap = Layer.makeMemoMapUnsafe(), scope = Scope.makeUnsafe()
  const http = createServer()
  try {
    const toolContext = await Effect.runPromise(Layer.buildWithMemoMap(AppNodeBuilder.build(ApplicationTools.node), memoMap, scope))
    const tools = makeTools(runtime)
    navigationRuntime = runtime; navigationTools = tools
    await Effect.runPromise(Context.get(toolContext, ApplicationTools.Service).register(tools)
      .pipe(Effect.provideService(Scope.Scope, scope)))
    const layer = HttpRouter.serve(HttpApiApp.createRoutes(), {
      middleware: disposeMiddleware, disableLogger: true, disableListenLog: true,
    }).pipe(
      Layer.provideMerge(AppNodeBuilder.build(WebSocketTracker.node)),
      Layer.provideMerge(NodeHttpServer.layer(() => http, {port: 0, host: "127.0.0.1", gracefulShutdownTimeout: "1 second"})),
      Layer.provide(ConfigProvider.layer(ConfigProvider.fromEnv())),
    )
    const context = await Effect.runPromise(Layer.buildWithMemoMap(layer, memoMap, scope).pipe(Effect.provide(HttpApiApp.context)))
    const address = Context.get(context, HttpServer.HttpServer).address
    if (address._tag !== "TcpAddress") throw new Error("Native listener unavailable")
    let closed = false
    return {
      port: address.port,
      async stop(force = false) {
        if (closed) return
        closed = true
        if (force) {
          http.closeAllConnections()
          await Effect.runPromise(Context.get(context, WebSocketTracker.Service).closeAll)
        }
        await Effect.runPromise(Scope.close(scope, Exit.void))
      },
    }
  } catch (error) {
    http.closeAllConnections()
    await Effect.runPromise(Scope.close(scope, Exit.void))
    throw error
  }
}
