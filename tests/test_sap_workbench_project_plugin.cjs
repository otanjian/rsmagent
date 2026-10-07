/**
 * The project plugin is the *caller* of the group-4 bridge channel, so what it
 * sends and what it refuses to send is part of the security boundary:
 *  - it must take the session id from the OpenCode context, never from tool args;
 *  - its argument surface must be exactly the transaction code;
 *  - without a configured bridge it must register no tool at all, rather than a
 *    tool that always fails.
 *
 * The plugin is ESM, but a `.js` file with no neighbouring package.json is
 * CommonJS to Node. Each case therefore imports a fresh `.mjs` copy, which also
 * gives each case its own module-level env read.
 */
const assert = require("node:assert/strict")
const fs = require("node:fs")
const os = require("node:os")
const path = require("node:path")
const test = require("node:test")
const {pathToFileURL} = require("node:url")

const SOURCE = path.join(__dirname, "..", "Scene", "sap_workbench", "project",
                         "plugins", "rsm-sap-workbench-navigation.js")

let counter = 0
async function loadPlugin({bridge} = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "rsm-plugin-"))
  const copy = path.join(dir, `plugin-${counter++}.mjs`)
  fs.copyFileSync(SOURCE, copy)
  if (bridge === undefined) delete process.env.RSM_SAP_WORKBENCH_BRIDGE_URL
  else process.env.RSM_SAP_WORKBENCH_BRIDGE_URL = bridge
  return (await import(pathToFileURL(copy).href)).default
}

function stubFetch(implementation) {
  const original = globalThis.fetch
  globalThis.fetch = implementation
  return () => { globalThis.fetch = original }
}

/** A tool context whose permission request is recorded, not prompted. */
function toolContext({sessionID, messageID} = {}) {
  return {
    sessionID, messageID, abort: undefined,
    asked: [],
    async ask(request) { this.asked.push(request) },
  }
}

test('page reads use the context session, empty args and fresh calls within one message', async () => {
  const plugin = await loadPlugin({bridge:'http://127.0.0.1:9900/bridge/'});
  const definition = (await plugin.server()).tool.sap_page_read;
  assert.deepEqual(definition.args, {});
  const context = toolContext({sessionID:'ses_rsm_x', messageID:'same-message'});
  const seen = [];
  const restore = stubFetch(async (url, options) => {
    seen.push(JSON.parse(options.body));
    assert.equal(url, 'http://127.0.0.1:9900/bridge/read');
    assert.match(options.headers.Authorization, /^Basic /);
    return {ok:true, json:async()=>({output:'{"scope":"rendered_dom"}'})};
  });
  try {
    assert.match(await definition.execute({},context), /rendered_dom/);
    await definition.execute({},context);
    assert.notEqual(seen[0].call_id, seen[1].call_id);
    assert.equal(seen[0].session_id, 'ses_rsm_x');
    assert.deepEqual(Object.keys(seen[0]).sort(), ['call_id','session_id']);
    assert.equal(context.asked[0].permission, 'sap_page_read');
    await definition.execute({session_id:'someone',script:'arbitrary'},context);
    assert.equal(seen.length, 2);
  } finally {restore();}
});

test('unsupported page reads report a refusal without substituting business data', async () => {
  const plugin = await loadPlugin({bridge:'http://127.0.0.1:9900/bridge'});
  const restore = stubFetch(async()=>({ok:false,json:async()=>({code:'page_read_unsupported'})}));
  try {
    assert.match(await (await plugin.server()).tool.sap_page_read.execute({},toolContext({sessionID:'ses_rsm_x'})), /page_read_unsupported/);
  } finally {restore();}
});

test('disabled reading explains the configuration rather than inferring the desktop OS', async () => {
  const plugin = await loadPlugin({bridge:'http://127.0.0.1:9900/bridge'});
  const definition = (await plugin.server()).tool.sap_page_read;
  assert.match(definition.description, /服务端操作系统/);
  const restore = stubFetch(async()=>({ok:false,json:async()=>({code:'page_read_disabled'})}));
  try {
    const result = await definition.execute({},toolContext({sessionID:'ses_rsm_x'}));
    assert.match(result, /开关未开启/);
    assert.match(result, /不是操作系统不支持/);
  } finally {restore();}
});

test("without a configured bridge the plugin registers no tool", async () => {
  const plugin = await loadPlugin({bridge: undefined})
  assert.equal(plugin.id, "rsm-sap-workbench-navigation")
  assert.deepEqual(await plugin.server(), {})
})

test("the only tool argument is the transaction code", async () => {
  const plugin = await loadPlugin({bridge: "http://127.0.0.1:9900/bridge"})
  const hooks = await plugin.server()
  const definition = hooks.tool.sap_transaction_open
  assert.ok(definition, "sap_transaction_open is registered")
  assert.deepEqual(Object.keys(definition.args), ["transaction"])
  // Plain JSON Schema, so the plugin needs no zod dependency: a zod field would
  // carry the `_zod` brand and the registry would take the zod path instead.
  assert.equal(definition.args.transaction._zod, undefined)
  assert.equal(definition.args.transaction.type, "string")
  assert.equal(typeof definition.execute, "function")
  // The description must not promise page contents or a business result.
  assert.match(definition.description, /不能/)
})

test("execute sends the OpenCode session id and reports delivery only", async () => {
  process.env.OPENCODE_SERVER_USERNAME = "opencode"
  process.env.OPENCODE_SERVER_PASSWORD = "secret-pass"
  const plugin = await loadPlugin({bridge: "http://127.0.0.1:9900/bridge"})
  const definition = (await plugin.server()).tool.sap_transaction_open
  let seen = null
  const restore = stubFetch(async (url, options) => {
    seen = {url, options}
    return {
      ok: true,
      status: 200,
      json: async () => ({output: JSON.stringify({status: "navigation_applied", transaction: "ME21N"})}),
    }
  })
  try {
    const result = await definition.execute({transaction: " me21n "},
      toolContext({sessionID: "ses_rsm_abc", messageID: "msg-1"}))
    assert.equal(seen.url, "http://127.0.0.1:9900/bridge")
    const body = JSON.parse(seen.options.body)
    // The session id comes from the context, and no identity field is invented.
    assert.deepEqual(Object.keys(body).sort(), ["call_id", "session_id", "transaction"])
    assert.equal(body.session_id, "ses_rsm_abc")
    assert.equal(body.transaction, "me21n")
    assert.match(seen.options.headers.Authorization, /^Basic /)
    const decoded = Buffer.from(seen.options.headers.Authorization.slice(6), "base64").toString()
    assert.equal(decoded, "opencode:secret-pass")
    assert.match(result, /已把导航发送到左侧/)
    assert.match(result, /未验证/)
  } finally { restore() }
})

test("a refusal is reported by its scene code, never as a business result", async () => {
  const plugin = await loadPlugin({bridge: "http://127.0.0.1:9900/bridge"})
  const definition = (await plugin.server()).tool.sap_transaction_open
  const restore = stubFetch(async () => ({ok: false, status: 403, json: async () => ({code: "session_not_bound"})}))
  try {
    const result = await definition.execute({transaction: "ME21N"}, toolContext({sessionID: "ses_rsm_x", messageID: "m"}))
    assert.match(result, /session_not_bound/)
    assert.doesNotMatch(result, /成功|已创建|已保存|已过账/)
  } finally { restore() }
})

test("an unreachable bridge is reported as not delivered", async () => {
  const plugin = await loadPlugin({bridge: "http://127.0.0.1:9900/bridge"})
  const definition = (await plugin.server()).tool.sap_transaction_open
  const restore = stubFetch(async () => { throw new Error("synthetic connection refused") })
  try {
    const result = await definition.execute({transaction: "ME21N"}, toolContext({sessionID: "ses_rsm_x", messageID: "m"}))
    assert.match(result, /未送达|桥接不可达/)
    assert.doesNotMatch(result, /synthetic/)
  } finally { restore() }
})

test("an empty transaction is refused before any request", async () => {
  const plugin = await loadPlugin({bridge: "http://127.0.0.1:9900/bridge"})
  const definition = (await plugin.server()).tool.sap_transaction_open
  let called = false
  const restore = stubFetch(async () => { called = true })
  try {
    const context = toolContext({sessionID: "ses_rsm_x"})
    const result = await definition.execute({transaction: "   "}, context)
    assert.match(result, /未提供事务码/)
    assert.equal(called, false)
    // Refused before the permission request too: nothing to approve.
    assert.deepEqual(context.asked, [])
  } finally { restore() }
})

test("execute asks the permission layer for the SAP tool before calling the bridge", async () => {
  const plugin = await loadPlugin({bridge: "http://127.0.0.1:9900/bridge"})
  const definition = (await plugin.server()).tool.sap_transaction_open
  let seen = null
  const restore = stubFetch(async (url, options) => {
    seen = {url, options}
    return {ok: true, status: 200, json: async () => ({output: "{}"})}
  })
  try {
    const context = toolContext({sessionID: "ses_rsm_x", messageID: "m"})
    await definition.execute({transaction: "me21n"}, context)
    // The visibility rule and the call approval are the same rule: the tool asks
    // for exactly the key the project config denies and the session profile
    // allows. No identity is involved, so a denial here is not an authorization
    // decision — the server still checks ownership.
    assert.equal(context.asked.length, 1)
    assert.equal(context.asked[0].permission, "sap_transaction_open")
    assert.deepEqual(context.asked[0].patterns, ["ME21N"])
    assert.ok(seen, "the bridge was still called once the ask resolved")
  } finally { restore() }
})

// --- the business-data tool -----------------------------------------------
//
// Same boundary as the navigation tool, plus one of its own: this tool really
// does reach SAP business data, and `call_rfc` can change it. Two claims must
// therefore stay absent from anything it returns -- a page readback, and a
// business conclusion drawn from a transport result.

function dataBridgeResponse(output) {
  return {ok: true, status: 200, json: async () => ({output})}
}

test("the data tool exposes business arguments but no connection or identity field", async () => {
  const plugin = await loadPlugin({bridge: "http://127.0.0.1:9900/bridge"})
  const definition = (await plugin.server()).tool.sap_data_call
  assert.ok(definition, "sap_data_call is registered")
  // The connection is not a model choice: it is a constant in the plugin and is
  // re-validated server-side. Only the tool and its business arguments remain.
  assert.deepEqual(Object.keys(definition.args).sort(), ["arguments", "tool"])
  assert.equal(definition.args.tool._zod, undefined)
  assert.equal(definition.args.tool.type, "string")
  assert.equal(definition.args.arguments.type, "object")
  // The two things the model most easily gets wrong stay stated.
  assert.match(definition.description, /sap_page_read/)
  assert.doesNotMatch(definition.description, /任何工具都读不到/)
  assert.match(definition.description, /不等于/)
})

test("the data tool sends the session id, the fixed connection and the business arguments", async () => {
  const plugin = await loadPlugin({bridge: "http://127.0.0.1:9900/bridge"})
  const definition = (await plugin.server()).tool.sap_data_call
  let seen = null
  const restore = stubFetch(async (url, options) => {
    seen = {url, options}
    return dataBridgeResponse('{"EBELN":"4500000127"}')
  })
  try {
    const result = await definition.execute(
      {tool: "read_table", arguments: {table_name: "EKKO", fields: "EBELN", row_count: 1}},
      toolContext({sessionID: "ses_rsm_abc", messageID: "msg-1"}))
    assert.equal(seen.url, "http://127.0.0.1:9900/bridge/data")
    const body = JSON.parse(seen.options.body)
    // Exactly the fields the server schema admits -- an invented identity field
    // would be rejected as `invalid_request`, not ignored.
    assert.deepEqual(Object.keys(body).sort(),
                     ["arguments", "call_id", "connection", "session_id", "tool"])
    assert.equal(body.session_id, "ses_rsm_abc")
    assert.equal(body.connection, "sap-pyrfc")
    assert.equal(body.tool, "read_table")
    assert.deepEqual(body.arguments, {table_name: "EKKO", fields: "EBELN", row_count: 1})
    assert.match(seen.options.headers.Authorization, /^Basic /)
    // The transport payload is passed through as-is: no page readback is added.
    assert.equal(result, '{"EBELN":"4500000127"}')
  } finally { restore() }
})

test("a data refusal keeps its scene code and is never a business conclusion", async () => {
  const plugin = await loadPlugin({bridge: "http://127.0.0.1:9900/bridge"})
  const definition = (await plugin.server()).tool.sap_data_call
  const restore = stubFetch(async () => ({ok: false, status: 403,
                                          json: async () => ({code: "mcp_action_forbidden"})}))
  try {
    const result = await definition.execute({tool: "adt_activate", arguments: {x: 1}},
      toolContext({sessionID: "ses_rsm_x", messageID: "m"}))
    assert.match(result, /mcp_action_forbidden/)
    assert.match(result, /不是业务结果/)
    assert.doesNotMatch(result, /已创建|已过账|已审批|成功/)
  } finally { restore() }
})

test("an unreachable data bridge is reported as not delivered", async () => {
  const plugin = await loadPlugin({bridge: "http://127.0.0.1:9900/bridge"})
  const definition = (await plugin.server()).tool.sap_data_call
  const restore = stubFetch(async () => { throw new Error("synthetic connection refused") })
  try {
    const result = await definition.execute({tool: "read_table", arguments: {table_name: "EKKO", row_count: 1}},
      toolContext({sessionID: "ses_rsm_x", messageID: "m"}))
    assert.match(result, /桥接不可达/)
    assert.doesNotMatch(result, /synthetic/)
  } finally { restore() }
})

test("a data call with no tool or no arguments is refused before any request", async () => {
  const plugin = await loadPlugin({bridge: "http://127.0.0.1:9900/bridge"})
  const definition = (await plugin.server()).tool.sap_data_call
  let called = false
  const restore = stubFetch(async () => { called = true })
  try {
    for (const args of [{tool: "   ", arguments: {}},      // blank tool
                        {tool: "read_table"},              // no arguments at all
                        {tool: "read_table", arguments: [1, 2]}]) {  // not an object
      const context = toolContext({sessionID: "ses_rsm_x"})
      assert.match(await definition.execute(args, context), /未提供/)
      // Refused before the permission request too: nothing to approve.
      assert.deepEqual(context.asked, [])
    }
    assert.equal(called, false)
  } finally { restore() }
})

test("the data tool asks for its own permission key, scoped to the tool name", async () => {
  const plugin = await loadPlugin({bridge: "http://127.0.0.1:9900/bridge"})
  const definition = (await plugin.server()).tool.sap_data_call
  const restore = stubFetch(async () => dataBridgeResponse("{}"))
  try {
    const context = toolContext({sessionID: "ses_rsm_x", messageID: "m"})
    await definition.execute({tool: "call_rfc", arguments: {function_name: "BAPI_PO_GETDETAIL1"}}, context)
    assert.equal(context.asked.length, 1)
    // A distinct key from the navigation tool: the session profile grants the
    // two separately, so a data call is not approved by a navigation approval.
    assert.equal(context.asked[0].permission, "sap_data_call")
    assert.deepEqual(context.asked[0].patterns, ["call_rfc"])
  } finally { restore() }
})

test("the ledger id distinguishes the arguments, not just the tool name", async () => {
  const plugin = await loadPlugin({bridge: "http://127.0.0.1:9900/bridge"})
  const definition = (await plugin.server()).tool.sap_data_call
  const seen = []
  const restore = stubFetch(async (url, options) => {
    seen.push(JSON.parse(options.body))
    return dataBridgeResponse("[]")
  })
  try {
    const context = toolContext({sessionID: "ses_rsm_x", messageID: "msg-7"})
    const readTable = (table, extra = {}) => definition.execute(
      {tool: "read_table", arguments: {table_name: table, row_count: 1, ...extra}}, context)
    await readTable("EKKO")
    await readTable("EKPO")   // a different read is a different action
    await readTable("EKKO")   // the same read still shares one id, so it de-dupes
    assert.notEqual(seen[0].call_id, seen[1].call_id)
    assert.equal(seen[0].call_id, seen[2].call_id)
    // Key order must not invent a new action for identical arguments.
    await definition.execute({tool: "read_table", arguments: {row_count: 1, table_name: "EKKO"}}, context)
    assert.equal(seen[3].call_id, seen[0].call_id)
    // A different tool with the same arguments is also a different action.
    const other = toolContext({sessionID: "ses_rsm_x", messageID: "msg-7"})
    await definition.execute({tool: "run_query", arguments: {table_name: "EKKO", row_count: 1}}, other)
    assert.notEqual(seen[4].call_id, seen[0].call_id)
  } finally { restore() }
})
