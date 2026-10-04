/** Native OpenCode routes + an additive scene tool, sharing one registry.
 * Uses the installed engine's HTTP/WebSocket layers and configuration discovery.
 */
import { createServer } from "node:http"
import { mkdir, readFile, writeFile } from "node:fs/promises"
import { join } from "node:path"
import { createHash } from "node:crypto"
import { loadRuntime } from "./host"

let navigationRuntime: Awaited<ReturnType<typeof loadRuntime>> | undefined
let navigationTool: any

export async function legacyNavigationHooks(guidance: string) {
  if (!navigationRuntime || !navigationTool) throw new Error("Workbench navigation is unavailable")
  const {Effect, Tool} = navigationRuntime, {z} = await navigationRuntime.dependency("zod")
  return {
    "experimental.chat.system.transform": async (_input: unknown, output: {system: string[]}) => {output.system.push(guidance)},
    tool: {sap_transaction_open: {
      description: Tool.definition("sap_transaction_open", navigationTool).description,
      args: {transaction: z.string()},
      async execute(input: {transaction: string}, context: any) {
        if (!context.sessionID || !context.messageID || !context.callID) throw new Error("Missing trusted tool context")
        await context.ask({permission: "sap_transaction_open", patterns: [input.transaction], always: ["*"], metadata: {transaction: input.transaction}})
        const result = await Effect.runPromise(Tool.settle(navigationTool,
          {id: context.callID, name: "sap_transaction_open", input},
          {sessionID: context.sessionID, agent: context.agent, assistantMessageID: context.messageID, toolCallID: context.callID}), {signal: context.abort})
        if (typeof result.structured !== "string") throw new Error("Invalid navigation response")
        return result.structured
      },
    }},
  }
}

async function installNavigationGuidance(directory: string) {
  const content = await readFile(new URL("./navigation-guidance.js", import.meta.url), "utf8")
  const plugins = join(directory, ".opencode", "plugins"), target = join(plugins, "rsm-sap-workbench-navigation.js")
  await mkdir(plugins, {recursive: true})
  try { await writeFile(target, content, {flag: "wx"}) }
  catch (error: any) {
    if (error.code !== "EEXIST") throw error
    const previous = await readFile(target, "utf8")
    if (previous !== content) {
      // Upgrade the exact first version installed by this change; never
      // replace a user's different file with the same plugin name.
      if (createHash("sha256").update(previous).digest("hex") !== "a4d3587833ee220f3f64e9271acccc386ecbe6a03774adbc9931c2f19635af32") throw error
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
    navigationRuntime = runtime; navigationTool = tools.sap_transaction_open
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
