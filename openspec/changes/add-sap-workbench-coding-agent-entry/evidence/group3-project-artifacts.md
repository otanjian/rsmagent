# 证据：第 3 组（项目目录产物：SAP skill 与插件，幂等安装）

日期：2026-10-05。改动文件：`Scene/sap_workbench/project/skills/sap-workbench/`（SKILL.md + 2 references）、`Scene/sap_workbench/project/plugins/rsm-sap-workbench-navigation.js`、`Scene/sap_workbench/backend/project_toolkit.py`、`Scene/sap_workbench/backend/http.py`、`Scene/sap_workbench/opencode_adapter/native-host.ts`、`scripts/start-opencode-web.ps1`、`tests/test_sap_workbench_project_toolkit.py`、`tests/test_sap_workbench_project_plugin.{py,cjs}`、`tests/test_sap_workbench.py`、`Scene/sap_workbench/opencode_adapter/native-host-guidance.test.ts`。

## 目的

对话由平台**标准**编码会话承载之后，知识（能力边界）与导航工具必须由该服务**自己**
发现，而不能依赖场景自管宿主在会话开始时注入。标准 OpenCode 的发现规则（已逐行核对
`rsmCode/opencode`）：

| 产物 | 发现位置 | 依据 |
|---|---|---|
| skill | 配置目录下 `{skill,skills}/**/SKILL.md` | `skill/index.ts:24,207`；`.opencode` 在配置目录内 `config/paths.ts:29,35` |
| 插件 | 配置目录下 `{plugin,plugins}/*.{js,ts}`，**自动发现，无需在配置里登记** | `config/plugin.ts:21`，调用点 `config/config.ts:478` |

因此落点是 `<project>/.opencode/skills/sap-workbench/SKILL.md` 与
`<project>/.opencode/plugins/rsm-sap-workbench-navigation.js`。

## 实现

### skill（3.1 / 3.3）

`SKILL.md`（frontmatter `name: sap-workbench` + description，触发条件写的是"要求打开事务码 /
读取 SAP 数据"）覆盖四类内容：能力边界、凭据纪律、打开事务码、只读读取与禁止动作。

- `references/transactions.md`：事务码写法、常见清单，以及"打开事务码 ≠ 有权限"；
- `references/boundaries.md`：逐条记录边界**为什么**成立（跨域 iframe、单向投递、只读≠校验、
  无提交适配器），并给出"只能靠观察页面完成 ⇒ 即不可用"的经验规则。

### 插件（3.2）

标准 OpenCode 插件（`export default {id, async server()}` 返回 `Hooks`，
`plugin/index.ts:115` 的 `readV1Plugin` 路径），而不是旧的宿主编排插件。

三个刻意的选择：

1. **零依赖**。项目目录下没有 `node_modules`，裸 `import "zod"` 无法解析。注册表对非 zod
   条目走 `legacyJsonSchema` 分支（`tool/registry.ts:131-134,363`），因此把 `args` 声明为
   **纯 JSON Schema** 即可，插件不需要 zod。与既有 `navigation-guidance.js` 刻意不裸 import
   的做法一致。
2. **身份只来自上下文**。`session_id` 取 `context.sessionID`，请求体键集固定为
   `{session_id, transaction, call_id}`——模型的参数面里根本没有用户、SAP 账号或绑定，
   连"以为选了某个 owner"都做不到（与第 4 组同一原则）。
3. **不重复注入提示**。知识只在 skill 里维护一份，插件只注册工具（任务 3.4 要求两处不得
   冲突，两份文案必然漂移）。工具 description 只声明"已投递、未验证"，与本组 skill 一致。

未配置 `RSM_SAP_WORKBENCH_BRIDGE_URL` 时 `server()` 返回 `{}`：注册一个必然失败的工具只会
误导模型。地址由启动脚本注入（`scripts/start-opencode-web.ps1`，新增 `-WorkbenchAppPort`），
凭据复用服务进程内已有的 `OPENCODE_SERVER_USERNAME` / `OPENCODE_SERVER_PASSWORD`。

### 幂等安装（3.1）

`backend/project_toolkit.py` 与旧 `installNavigationGuidance` 同源（同一目标文件名，迁移必须
能替换它）：不存在 → 写入；内容相同（忽略 BOM/行尾）→ 保留；是**我们发布过的修订** → 升级；
别的内容 → **拒绝覆盖**并报告。安装发生在保存配置时（`http.py` PUT），且**非致命**：项目
目录不可达仍是合法配置，报告如实返回，界面不得据此宣称能力可用。

## 发现并修掉的两个迁移缺陷

这两个都是"看起来能跑、但在真实部署上会坏"的问题，只在核对两侧摘要时暴露：

1. **已部署主机上的旧插件会被拒绝替换。** 旧 `upgradeableGuidance` 里登记的两个摘要，是
   **更早**被替换的修订；旧宿主**今天**实际安装的是当前 `navigation-guidance.js`
   （摘要 `0412f4ef…`）。不登记它，迁移会把宿主自己的文件读成"用户改动"而拒绝替换，
   留下一个宿主态插件。
2. **部署顺序会变成正确性前提。** `installNavigationGuidance` 遇到不认识的同名文件会
   **抛错并阻断工作台宿主启动**。所以在第 2 组退役宿主之前，新插件会先把旧宿主搞崩。
   已把新插件摘要（`358a9018…`）登记进旧集合：**两侧各自接受对方的修订**，无论谁先落盘都
   不会阻塞启动，部署顺序不再是前提。

两个摘要都硬编码为字面量，因为它们必须**比其来源文件活得更久**（第 7 组会删掉旧文件）。

## 测试

| 范围 | 结果 |
|---|---|
| `tests/test_sap_workbench_project_toolkit.py` | 10 passed（全新安装、幂等无重复、CRLF 同修订、旧修订升级、用户改动冲突不覆盖、目录缺失拒绝、嵌套目录、来源齐全、旧宿主修订迁移） |
| `tests/test_sap_workbench_project_plugin.py`（node:test，6 例） | 6 passed（无地址不注册工具、参数面只有事务码且为纯 JSON Schema、session_id 取自上下文且凭据为 Basic、拒绝按场景 code 回报、桥接不可达报告未送达、空事务码不发请求） |
| `tests/test_sap_workbench.py` 新增 2 例 | passed（保存绑定把 skill/插件装进项目且二次保存为 unchanged；项目目录不可达不导致保存失败） |
| `native-host-guidance.test.ts`（bun） | 7 passed，含新增的双向兼容用例 |
| `tests/test_sap_workbench.py` + `..._access.py` 全量 | 53 passed，2 failed |

### 两个失败用例，均为既有环境问题，非本次改动引入

`tests/test_sap_workbench.py::test_session_rejects_invalid_request_key_and_password`、
`::test_platform_agent_routing_metadata_is_ignored`：
`OSError [Errno 10048] bind 127.0.0.1:9911 已被占用`。9911 由**正在运行的真实应用**
（`python -u app.py`，pid 9784）占用，测试用的是固定代理端口。失败发生在**启动 screen
gateway**时，早于任何本次改动的代码路径；同一测试里的配置保存断言（`version == 1`）已通过，
即 PUT 未被安装步骤破坏。

## 未覆盖 / 下一步

- **未做真实服务内的发现验证**。发现规则是逐行核对源码得到的，尚无"把真实项目目录交给
  一个运行中的标准服务，再观察 skill 被列出、`sap_transaction_open` 出现在工具面"的端到端
  证据。属于第 8 组。
- 插件的 `ask`（原生权限确认）**未启用**。第 5 组配置权限规则时再决定是否开启；服务端授权
  才是真正的门，权限规则只用于减少误调用。
- 安装只发生在保存配置时。第 2 组开放会话语义落地后，若配置已存但安装曾失败，开放路径是否
  重试需要一并确认。
