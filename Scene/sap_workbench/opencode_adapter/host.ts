/** SAP-only composition of the installed OpenCode routes and canonical tools.
 * No monkey patch, legacy plugin registration, or changes to the OpenCode tree.
 * The owner must put an authenticated, session-scoped gateway in front of fetch.
 */
import { resolve } from "node:path"

export async function loadRuntime(root: string) {
  const base = resolve(root, "packages/opencode")
  const dependency = (name: string) => import(Bun.resolveSync(name, base))
  const source = (name: string) => import(resolve(root, name))
  const effect = await dependency("effect")
  const http = await dependency("effect/unstable/http")
  const { AppNodeBuilder } = await source("packages/core/src/effect/app-node-builder.ts")
  const { ApplicationTools } = await source("packages/core/src/tool/application-tools.ts")
  const { Tool } = await source("packages/core/src/tool/tool.ts")
  await source("packages/opencode/src/server/init-projectors.ts")
  const { HttpApiApp } = await source("packages/opencode/src/server/routes/instance/httpapi/server.ts")
  return { ...effect, ...http, AppNodeBuilder, ApplicationTools, Tool, HttpApiApp, source, dependency }
}

export async function createHost(runtime: Awaited<ReturnType<typeof loadRuntime>>, tools: Record<string, unknown>, configuration?: unknown) {
  const { Effect, Context, Layer, Scope, Exit, HttpRouter, AppNodeBuilder, ApplicationTools, HttpApiApp } = runtime
  // The same memo map is essential: a separately built SDK or CLI would create
  // another ApplicationTools service, invisible to the real Session V2 runner.
  const memoMap = Layer.makeMemoMapUnsafe()
  const scope = Scope.makeUnsafe()
  const context = await Effect.runPromise(
    Layer.buildWithMemoMap(AppNodeBuilder.build(ApplicationTools.node), memoMap, scope),
  )
  const registry = Context.get(context, ApplicationTools.Service)
  try {
    await Effect.runPromise(registry.register(tools).pipe(Effect.provideService(Scope.Scope, scope)))
    const routes = configuration ? await scopedRoutes(runtime, configuration) : HttpApiApp.createRoutes()
    const web = HttpRouter.toWebHandler(routes, { disableLogger: true, memoMap })
    let closed = false
    return {
      fetch: (request: Request) => closed
        ? Promise.resolve(new Response("Service closed", { status: 503 }))
        : web.handler(request, HttpApiApp.context),
      async close() {
        if (closed) return
        closed = true
        try { await web.dispose() }
        finally { await Effect.runPromise(Scope.close(scope, Exit.void)) }
      },
    }
  } catch (error) {
    await Effect.runPromise(Scope.close(scope, Exit.void))
    throw error
  }
}

async function scopedRoutes(runtime: Awaited<ReturnType<typeof loadRuntime>>, configuration: unknown) {
  const { Effect, Layer, Option, Schema, AppNodeBuilder, source, dependency, HttpServer } = runtime
  const { HttpApiBuilder } = await dependency("effect/unstable/httpapi")
  const { LayerNode } = await source("packages/core/src/effect/layer-node.ts")
  const { Config } = await source("packages/core/src/config.ts")
  const { ConfigMigrateV1 } = await source("packages/core/src/v1/config/migrate.ts")
  const info = Schema.decodeUnknownSync(Config.Info)(ConfigMigrateV1.isV1(configuration) ? ConfigMigrateV1.migrate(configuration) : configuration)
  // Explicit config service replacement is a supported graph-composition
  // seam. The real project stays the session location, but its .opencode
  // files cannot replace tools, providers, credentials or permission rules.
  const fixed = Layer.succeed(Config.Service, Config.Service.of({
    entries: () => Effect.succeed([new Config.Document({type: "document", path: "sap-workbench", info})]),
  }))
  const specs = [
    ["database/database", "Database"], ["event", "EventV2"], ["session", "SessionV2"],
    ["permission/saved", "PermissionSaved"], ["pty/ticket", "PtyTicket"],
    ["credential", "Credential"], ["location-service-map", "LocationServiceMap"],
  ]
  const nodes = await Promise.all(specs.map(async ([path, name]) => (await source(`packages/core/src/${path}.ts`))[name].node))
  const { httpClient } = await source("packages/core/src/effect/app-node-platform.ts")
  const { PtyEnvironment } = await source("packages/server/src/pty-environment.ts")
  const { SessionExecution } = await source("packages/core/src/session/execution.ts")
  const { SessionExecutionLocal } = await source("packages/core/src/session/execution/local.ts")
  const { Api } = await source("packages/server/src/api.ts")
  const { handlers } = await source("packages/server/src/handlers.ts")
  const { ServerAuth } = await source("packages/server/src/auth.ts")
  const { authorizationLayer } = await source("packages/server/src/middleware/authorization.ts")
  const { schemaErrorLayer } = await source("packages/server/src/middleware/schema-error.ts")
  const { sessionLocationLayer } = await source("packages/server/src/middleware/session-location.ts")
  const { layer: locationLayer } = await source("packages/server/src/location.ts")
  const services = AppNodeBuilder.build(LayerNode.group([...nodes, httpClient, PtyEnvironment.node]), [
    [Config.node, fixed], [SessionExecution.node, SessionExecutionLocal.node],
  ])
  return HttpApiBuilder.layer(Api).pipe(Layer.provide(handlers), Layer.provide(sessionLocationLayer),
    Layer.provide(locationLayer), Layer.provide(authorizationLayer), Layer.provide(schemaErrorLayer),
    Layer.provide(ServerAuth.Config.configLayer({username: "opencode", password: Option.none()})),
    Layer.provide(services), Layer.provide(HttpServer.layerServices))
}
