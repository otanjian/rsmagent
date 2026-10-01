## Why

控制台的侧边栏与页面标题把该页显示为「外部系统接入」，而它承载的是 MCP、ERP、OA 与本人邮箱的统一连接目录。`console-information-architecture` 的「外部系统接入位于模型与接入分组」明确要求「导航、页标题、面包屑及场景跳转 SHALL 一致」，因此显示名分散在四处，任何一处漏改都会直接违反这条要求，而当前**没有任何用例**守住它：

| 位置 | 现值 |
|---|---|
| `i18n` `menu_external_connections`（zh / zh-Hant / en） | 外部系统接入 / 外部系統接入 / External System Access |
| `i18n` `ec_title`（三种语言） | 同上，见 `channel/web/static/js/i18n/external-connections.js:10`、`:11`、`:257`、`:258`、`:504`、`:505` |
| `channel/web/chat.html:344` | 菜单项的静态回退文本（`data-i18n` 未命中时显示） |
| `auth/service.py:126` | `_SIGNED_CONSOLE_PAGES` 的 `label`，经 `_resource_source_projection` 投影为角色分配界面里的资源 `name` |

`tests/test_external_connections_menu.py` 用 `PAGE` / `PAGE_GRANT` 常量定位该页，从不断言显示名；`tests/test_console_i18n_parity.cjs` 只比对每个语言的**键集合**（`Object.keys`），值可以自由变动。于是这四处可以各自改动而测试全绿 —— 这正是本 change 要补上的缺口。

## What Changes

- 该页的显示名 SHALL 为「系统接入」（English: `System Access`），SHALL 在侧边栏、页面标题、面包屑、场景跳转与角色分配的菜单资源名五处一致。
- 三种语言的 `menu_external_connections` 与 `ec_title` SHALL 同步；`chat.html` 的静态回退文本 SHALL 与之一致，使脚本未加载或 i18n 未命中时不闪回旧名。
- 能力标识、视图 id、路由、文件名与 `data-view="external_connections"` **保持不变**：本次只改显示名。

## Capabilities

### Modified Capabilities

- `console-information-architecture`：requirement「外部系统接入位于模型与接入分组」重命名为「系统接入位于模型与接入分组」，正文与场景改用新显示名，并新增场景把「三种语言 + 静态回退 + 角色资源名一致」钉住。

### 有意不改的引用

`external-system-access-console`（Purpose）、`console-settings-organization`、`personal-email-integration`、`erp-connection-integration` 中的「外部系统接入」是**能力概念词汇**（对应能力标识与文件名），不是显示名引用；能力标识按决定保持 `external-system-access-console` 不变，因此这些描述继续使用概念名，不随 UI 文案变动。OpenSpec 也明确禁止在既有能力的 delta 中携带 `## Purpose`。

## Impact

- **行为受影响**：侧边栏菜单、页面标题、角色权限页分配菜单时的资源名，以及三种语言下的该文案。
- **不受影响**：能力标识、视图 id、路由与地址、`data-view` 值、授权判定、筛选与统计口径、连接数据。
- **代码面**：`channel/web/static/js/i18n/external-connections.js`、`channel/web/chat.html`、`auth/service.py`；`scripts/seed_external_connections.py` 里建立的访问角色名同步为「系统接入」。提及该页名的注释（`console.js`、`external-connections.js`、`console.css`）一并对齐，避免注释与界面叫法不一致。
- **测试面**：新增守住显示名的用例（前端三语言一致性 + 服务端菜单资源名），并同步 `tests/fixtures/console_i18n_snapshot.json` 的对应值。
- **未覆盖**：`openspec/changes/archive/**` 保持原样（归档记录不改）。
