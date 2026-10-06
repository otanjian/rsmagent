/**
 * SAP 工作台导航工具（标准 OpenCode 插件）。
 *
 * 安装位置：`<project>/.opencode/plugins/rsm-sap-workbench-navigation.js`
 * 由 OpenCode 自动发现（`ConfigPlugin.load` 扫描配置目录下的 `{plugin,plugins}/*.{js,ts}`），
 * 不需要在项目配置里登记，也不需要场景专用构建。
 *
 * 这是第 4 组服务端通道的**调用方**：工具执行时把 OpenCode 会话标识与事务码
 * POST 到场景桥接端点。会话标识由 OpenCode 运行时注入（`context.sessionID`），
 * **不是**工具参数——模型无法声明自己是谁，归属只由服务端按会话标识反查。
 *
 * 为什么没有工具参数之外的身份字段：见 `SKILL.md` 与场景设计。工具参数只接受
 * 事务码，任何用户/SAP 账号/绑定标识都不出现在参数面上。
 *
 * 为什么 `args` 是纯 JSON Schema 而不是 zod：
 * 项目目录下没有 `node_modules`，裸 `import "zod"` 无法解析；而注册表
 * (`packages/opencode/src/tool/registry.ts`) 对非 zod 的条目会走 `legacyJsonSchema`
 * 分支直接使用 JSON Schema。这样插件**零依赖**，与既有 `navigation-guidance.js`
 * 刻意不裸 import 的做法一致。
 *
 * 为什么这里不注入系统提示：知识已由随场景安装的 `sap-workbench` skill 承载。
 * 任务 3.4 要求两处描述不得冲突，因此插件只注册工具，能力边界只在 skill 里维护
 * 一份。
 */

const BRIDGE_URL = process.env.RSM_SAP_WORKBENCH_BRIDGE_URL

/** 服务端业务工具面指向的已登记连接。写死在这里而不是交给模型：多一个恒定字段只会
 * 多一处可以填错的地方，而服务端仍会独立校验 connection（`mcp_business.CONNECTION`）。 */
const CONNECTION_ID = "sap-pyrfc"

/** 桥接端点按编码服务自身凭据认证调用方（同机直连、无转发头）。 */
function bridgeHeaders() {
  const username = process.env.OPENCODE_SERVER_USERNAME ?? ""
  const password = process.env.OPENCODE_SERVER_PASSWORD ?? ""
  const basic = Buffer.from(`${username}:${password}`, "utf8").toString("base64")
  return {"Content-Type": "application/json", Authorization: `Basic ${basic}`}
}

/**
 * 工具调用的账本标识。同一助手消息内对同一事务码的重复调用会得到同一个标识，
 * 因此服务端按 (绑定, 标识) 去重；不同事务码或不同消息各自独立。
 */
function ledgerCall(messageID, transaction) {
  const scope = typeof messageID === "string" && messageID ? messageID : `anon-${Date.now()}`
  return `${scope}:${transaction}`.slice(0, 128)
}

export const transactionOpenTool = {
  description: [
    "在当前 SAP 工作台左侧画面中打开一个事务码（例如 ME21N、ME23N、SPRO）。",
    "这是当前工作台的导航工具：它只把导航指令送达左侧画面，并回报“已送达”。",
    "它**不能**确认 SAP 是否已登录、用户是否有权限、事务标题是否正确或单据状态，",
    "也**不能**读取页面上的字段、表格或消息。请勿据此声称看过左侧页面。",
    "通用浏览器工具操作的是另一个浏览器，不会改变用户的左侧画面。",
  ].join(""),
  // 纯 JSON Schema：注册表对非 zod 条目直接使用它，插件因此不需要 zod 依赖。
  args: {
    transaction: {
      type: "string",
      description: "SAP 事务码，例如 ME21N 或 /N/ME23N。不要传入 URL、主机或客户端号。",
    },
  },
  async execute(args, context) {
    const transaction = typeof args?.transaction === "string" ? args.transaction.trim() : ""
    if (!transaction) return "未提供事务码，未发送任何导航。"
    // 可见性由权限规则决定：项目配置默认 `deny` 会对普通编码会话隐藏本工具，场景
    // 发起的会话由平台入口下发会话级 `allow` 重新放开。这里再走一次原生权限请求，
    // 让“可见”与“可调用”经过同一套规则。这只是减少误调用——真正的授权由服务端按
    // 会话归属校验，权限规则不是授权依据。
    await context.ask({
      permission: "sap_transaction_open",
      patterns: [transaction.toUpperCase()],
      always: ["*"],
      metadata: { transaction },
    })
    let payload
    try {
      const response = await fetch(BRIDGE_URL, {
        method: "POST",
        headers: bridgeHeaders(),
        body: JSON.stringify({
          session_id: context.sessionID,
          transaction,
          call_id: ledgerCall(context.messageID, transaction.toUpperCase()),
        }),
        signal: context.abort,
      })
      payload = await response.json().catch(() => null)
      if (!response.ok) {
        // 服务端的 code 是场景自有枚举，不含 SAP 响应或凭据；原样回报以便区分
        // “未绑定”“未运行”“事务码非法”，而不是笼统的失败。
        const code = payload && typeof payload.code === "string" ? payload.code : `http_${response.status}`
        return `未能把导航发送到左侧 SAP 画面（${code}）。这不是登录或业务结果，只是没有送达。`
      }
    } catch (error) {
      return `未能把导航发送到左侧 SAP 画面（桥接不可达）。这不是登录或业务结果，只是没有送达。`
    }
    const output = typeof payload?.output === "string" ? payload.output : ""
    try {
      const parsed = JSON.parse(output)
      const code = parsed?.transaction ?? transaction.toUpperCase()
      return `已把导航发送到左侧 SAP 画面（事务码 ${code}）。请查看左侧实际页面。未验证 SAP 登录、权限、事务标题或单据状态。`
    } catch {
      return `已把导航发送到左侧 SAP 画面（事务码 ${transaction.toUpperCase()}）。请查看左侧实际页面。未验证 SAP 登录、权限、事务标题或单据状态。`
    }
  },
}

/**
 * 参数的稳定摘要。服务端按 `(绑定, call_id)` 去重，重复标识会被
 * `action_already_dispatched` 拒绝；因此标识必须区分**参数**，不能只区分工具名——
 * 同一消息里读 EKKO 与读 EKPO 是两个不同动作。相同参数的重复调用仍共用标识，
 * 于是真正的重复请求照旧被去重。
 */
function stableStringify(value) {
  if (value === null || typeof value !== "object") return JSON.stringify(value) ?? "null"
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(",")}]`
  return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableStringify(value[key])}`).join(",")}}`
}

function fnv1a(text) {
  let hash = 0x811c9dc5
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index)
    hash = Math.imul(hash, 0x01000193) >>> 0
  }
  return hash.toString(16).padStart(8, "0")
}

function dataCallId(messageID, tool, business) {
  const scope = typeof messageID === "string" && messageID ? messageID : `anon-${Date.now()}`
  return `${scope}:${tool}:${fnv1a(stableStringify(business))}`.slice(0, 128)
}

/**
 * 业务数据调用工具。模型只传业务参数；连接、SAP 账号与口令全部留在服务端。
 *
 * 为什么参数面里没有连接标识：服务端按下发的会话标识解析绑定与该绑定的已登记
 * 连接，连接 id 直到 MCP worker 内部才被附加（`SapMcpLogin._call`）。模型既拿不到
 * 凭据，也拿不到连接 id —— 与导航通道同一条所有权规则。
 *
 * 为什么 `connection` 写死在插件里：服务端的业务工具面只指向一个已登记连接，让模型
 * 传一个恒定值只会多出一个可以填错的字段。
 */
export const dataCallTool = {
  description: [
    "经服务端已登记的 SAP 连接执行一次业务数据调用：可读取 SAP 业务数据，也可经 BAPI 写入。",
    "这**不是**读取左侧画面：左侧是跨域 iframe，任何工具都读不到它的字段、表格、标签页或消息。",
    "可用 tool 及其 arguments：",
    "read_table —— {table_name, fields?, where?, row_count, row_skip?}；",
    "run_query —— {sql_query, row_count}，仅允许单条只读 SELECT；",
    "call_rfc —— {function_name, parameters_json?}，可调用 BAPI（可能改变业务数据）。",
    "SAP 连接、账号与口令由服务端持有；参数里不接受 connection_id、user、password、client、host 或 url。",
    "返回值是 MCP 传输结果，不是业务结论：读到记录不等于单据已审批、已过账或未关闭；",
    "call_rfc 的传输成功也不代表 SAP 已完成该业务动作。不要据此声称已创建、已过账或已审批。",
  ].join(""),
  args: {
    tool: {
      type: "string",
      description: "read_table、run_query 或 call_rfc。",
    },
    arguments: {
      type: "object",
      description: "该 tool 的业务参数；不要传 connection_id、user、password、client、host 或 url。",
    },
  },
  async execute(args, context) {
    const tool = typeof args?.tool === "string" ? args.tool.trim() : ""
    const supplied = args?.arguments
    const business = supplied && typeof supplied === "object" && !Array.isArray(supplied) ? supplied : null
    if (!tool || !business) return "未提供 tool 或其业务参数，未发起任何调用。"
    // 与导航通道同一套规则：先走原生权限请求，让"可见"与"可调用"经过同一条路径。
    // 这只是减少误调用——真正的授权由服务端按会话归属校验，权限规则不是授权依据。
    await context.ask({
      permission: "sap_data_call",
      patterns: [tool],
      always: ["*"],
      metadata: { tool },
    })
    let payload
    try {
      const response = await fetch(`${BRIDGE_URL}/data`, {
        method: "POST",
        headers: bridgeHeaders(),
        body: JSON.stringify({
          session_id: context.sessionID,
          connection: CONNECTION_ID,
          tool,
          arguments: business,
          call_id: dataCallId(context.messageID, tool, business),
        }),
        signal: context.abort,
      })
      payload = await response.json().catch(() => null)
      if (!response.ok) {
        // 服务端的 code 是场景自有枚举，不含 SAP 响应或凭据；原样回报以便区分
        // "未绑定""未运行""参数被拒""SAP 调用失败"。
        const code = payload && typeof payload.code === "string" ? payload.code : `http_${response.status}`
        return `未能执行 SAP 业务调用（${code}）。这不是业务结果，只是没有送达。`
      }
    } catch (error) {
      return `未能执行 SAP 业务调用（桥接不可达）。这不是业务结果，只是没有送达。`
    }
    const output = typeof payload?.output === "string" ? payload.output : ""
    return output || "调用已送达，但服务端没有返回数据。"
  },
}

export default {
  id: "rsm-sap-workbench-navigation",
  async server() {
    // 未配置桥接地址时插件保持惰性：注册一个必然失败的工具只会误导模型。
    if (!BRIDGE_URL) return {}
    return {
      tool: {
        sap_transaction_open: transactionOpenTool,
        sap_data_call: dataCallTool,
      },
    }
  },
}
