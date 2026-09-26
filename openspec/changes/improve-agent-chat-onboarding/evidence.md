# 验收证据：improve-agent-chat-onboarding

日期：2026-09-26。范围：Web 普通单智能体（`agent_type=normal`）的空会话对话引导。Desktop 与 OpenCode 类智能体不在本 change 内。

## 1. 实施落点

| 层 | 文件 | 内容 |
| --- | --- | --- |
| 档案 | `agent/registry.py` | `usage_hint`（≤200 字）、`suggested_questions`（≤4 条，每条 ≤200 字）解析、清洗、序列化；`to_dict` / `_profile_from_mapping` 回读 |
| 管理 | `agent/admin.py` | `create_agent` / `update_agent` / `clone_agent` 纳入两字段；`_UNSET` 省略保留、显式空值清空的语义 |
| API/投影 | `channel/web/fork/handlers/agents.py` | create/update 载荷处理；`_tenant_agents_projection` 一并下发，避免客户端二次请求 |
| 表单 | `channel/web/static/js/console.js`、`static/js/i18n/agents.js` | 概况页新增使用说明与四个问题输入框，复用原保存流程 |
| 欢迎 | `channel/web/chat.html`、`static/js/console.js`、`static/js/i18n/home-scenes.js` | 头像/名称 → greeting → usage_hint → 建议问题 → 原输入框；纯文本渲染、默认回退 |
| 样式 | `channel/web/static/css/appearance.css` | 宽屏双列、`≤560px` 单列、紧凑留白 |

## 2. 自动化验证

### 2.1 Python（`pytest`，`.venv/bin/python -m pytest ... -q -p no:randomly`）

```
tests/test_agent_registry.py tests/test_agent_admin.py tests/test_agent_workbench.py tests/test_agent_clone_primitives.py
  → 114 passed

tests/test_agent_*.py tests/test_team*.py
  → 400 passed

tests/test_agent_web_management.py tests/test_tenant_agent_creation.py tests/test_agent_visibility_routes.py
  → 全部通过（与上条合并统计时为 31 passed / 0 failed）
```

覆盖：字段回读、空值与显式清空、超长/非法输入拒绝、旧档案缺省、管理 API 省略字段保存、复制保留、权限拒绝、revision 冲突、使用投影白名单。

### 2.2 前端（`node tests/<file>`）

| 套件 | 结果 |
| --- | --- |
| `test_agent_welcome_frontend.cjs` | pass 10 / fail 0 |
| `test_agent_onboarding_copy_wire.py`（新增，见 §3.1） | pass 6 / fail 0 |
| `test_agent_profile_frontend.cjs` | pass 8 / fail 0 |
| `test_console_i18n_parity.cjs` | pass 6 / fail 0 |
| `test_appearance_frontend.cjs` | pass 29 / fail 0 |
| `test_console_view_registry.cjs` | pass 7 / fail 0 |
| `test_admin_home_frontend.cjs` | pass 5 / fail 0 |
| `test_console_i18n_coverage.cjs` | pass 4 / fail 0 |
| `test_i18n_tenant_editor_keys.cjs` | pass 4 / fail 0 |
| `test_i18n_external_identity_keys.cjs` | pass 5 / fail 0 |
| `test_agent_chat_launch_frontend.cjs` | pass 16 / fail 0 |

`test_agent_welcome_frontend.cjs` 的 10 项即 2.3 / 3.3 的自动化部分：单智能体显示头像/名称/介绍/使用说明/四条问题、缺省回退、切换智能体不串文案、其他模式不套用、历史不重复介绍、语言切换只翻译标题不改写作者文案、点击只发送一次、有草稿或附件时禁用并给出原因、运行中禁用，以及引导态不再渲染品牌眉标与品牌标语。

### 3.1 关键回归：概况页「保存后内容消失」的根因与守卫

现象：在概况页填写「使用说明」并保存后，内容立即消失。

根因（已在证据中定位，非代码缺陷）：**运行中的服务进程早于本次后端改动启动**。

- 运行实例 `app.py` pid 31156 启动于 2026-09-26 08:41:16；`agent/registry.py`（16:46）、`agent/admin.py`（16:50）、`channel/web/fork/handlers/agents.py`（16:50）均在其后修改，进程未重启，因此加载的仍是改动前的 Python。
- 改动前的代码在 `POST /api/agents {action:"update"}` 中不含这两个字段，读取也不返回（`git show HEAD:agent/registry.py` 无 `usage_hint`），于是保存被静默丢弃。
- 概况页每次保存后都会 `loadAgentCatalog()` → `renderAgentDetail()`，用服务端返回值整块重建表单，因此忠实显示为空 —— 用户看到「内容又不见了」。

守卫测试 `tests/test_agent_onboarding_copy_wire.py`：走真实 WSGI 路由，覆盖 management 读、workbench 读、省略保留、显式清空、超长拒绝、五条/斜杠命令拒绝、创建即可携带。RED/GREEN 对照：

```
HEAD（运行中服务加载的版本）→ 6 failed；首个失败为 KeyError: 'usage_hint'
工作区（本次改动）          → 6 passed
```

结论：代码路径正确；让改动生效需要重启服务（见 §4 回退说明）。**Python 改动不会被热加载，静态资源（JS/CSS）会。**

### 3.2 引导态移除品牌眉标与品牌标语

按验收反馈，普通单智能体空会话不再显示 `容言AI · 你的工作助手`（`.home-eyebrow`）与品牌标语「有容乃大 · 智创未来」（`[data-brand-desc]`）：标题已为「你好，我是<智能体>」，两者均属重复装饰。仅作用于 `.agent-onboarding` 态，通用空会话与品牌预览保持原样；由 `test_agent_welcome_frontend.cjs` 的样式断言守卫。

### 2.3 语法检查

```
python -m py_compile agent/registry.py agent/admin.py channel/web/fork/handlers/agents.py   → OK
node --check channel/web/static/js/console.js
node --check channel/web/static/js/i18n/agents.js
node --check channel/web/static/js/i18n/home-scenes.js                                     → OK
```

### 2.4 OpenSpec

```
openspec validate improve-agent-chat-onboarding --strict
→ Change 'improve-agent-chat-onboarding' is valid
```

## 3. 真实浏览器布局验证（4.2）

运行实例：`app.py` pid 31156，`http://127.0.0.1:9899/chat`。静态资源按磁盘读取，故前端改动直接生效；后端 Python 改动需重启后生效。

由于实例处于登录门禁之后，验证方式为：在当前已加载页面内注入一个示例智能体目录并调用既有 `paintWelcomeAgentIntro()`，用 CDP `Emulation.setDeviceMetricsOverride` 切换视口后测量真实几何（`getBoundingClientRect` / `getComputedStyle`）。这不是替代首问端口到端验收，仅验证新 DOM/CSS 在真实样式表中的渲染与布局。

| 视口 | 建议问题栅格 | 引导区（top–bottom） | 输入框 top | 重叠 | 横向溢出 |
| --- | --- | --- | --- | --- | --- |
| 1440×900 | `375px 375px`（双列） | 415–553 | 580 | 无 | 无 |
| 1280×720 | `375px 375px`（双列） | 404–542 | 564 | 无 | 无 |
| 375×812 | `339px`（单列） | 440–695 | 720 | 无 | 无 |

另核对：注入示例后标题变为「你好，我是客户需求方案顾问」，`usage_hint` 文本渲染正确，通用六宫格建议区被隐藏，`#chat-main` 带 `agent-onboarding` 类；窄屏下四条问题单列堆叠、左内边距 18px、无水平滚动；页面在 1280×720 时由既有 `#chat-main` 纵向滚动容器承载（`scrollHeight 783 > clientHeight 656`），非本次改动引入。

截图证据：`evidence/onboarding-1440.png`、`evidence/onboarding-375.png`。

## 4. 兼容、回退与开关（4.3）

- **无新增 feature flag**：两字段随正常版本发布；未配置即走缺省中性文案。
- **旧配置兼容**：省略 `usage_hint` / `suggested_questions` 的旧档案与旧管理 API 载荷保留原值、不报错；`test_agent_registry.py` / `test_agent_admin.py` / `test_agent_onboarding_copy_wire.py` 覆盖。
- **生效方式**：Python 改动需重启进程（`app.py`）；静态资源无需重启。本次改动后若未重启，概况页保存会表现为「内容消失」（见 §3.1）。
- **正常版本回退**：撤回本次 UI 与字段增量即可；已运行会话不受影响。若回退到会在保存时丢弃未知字段的旧服务，先备份这两个字段（现存配置备份方式不变）。

## 5. 未完成项与缺口

- **3.3 的真实后端首问**：需在加载新后端代码（即重启后）的实例上登录后发送一条建议问题并确认回复。当前运行实例早于改动启动且无登录凭据，未执行，故 3.3 保持未勾选（自动化部分已通过）。
- **4.1 文案录入**：roster（`~/cow/agents/team.json`，745 条，含大量 pytest 临时残留）中仅 3 个非临时智能体命中六组目标名：

  | 目标名 | 智能体 id | 当前 greeting | usage_hint | suggested_questions |
  | --- | --- | --- | --- | --- |
  | 智能办公助理 | `my-assistant-admin`（默认） | 已有自定义 | 空 | 0 |
  | 经营分析参谋 | `business-analysis` | 已有自定义 | 空 | 0 |
  | 企业知识官 | `knowledge-qa` | 已有自定义 | 空 | 0 |
  | 客户需求方案顾问 | 仅 `*-test15` 租户副本 | 已有自定义 | 空 | 0 |
  | BUG 管家 | 仅 `bug-butler-test15` 租户副本 | 已有自定义 | 空 | 0 |
  | 税务健康体检 | 未找到非临时对象 | — | — | — |

  写入属生产数据变更且需操作者确认目标，故未执行；文案见 `onboarding-copy.md`，可由操作者按现状通过概况页或 `POST /api/agents` 录入（`greeting` 已自定义，仅补 `usage_hint` 与 `suggested_questions`）。

## 6. 已知无关失败

`tests/test_compat_surface_closure.py` 有 2 项失败，涉及 `agent/private_agent.py`、`agent/tools/requirements_delivery.py`、`auth/service.py` 的能力开关读取方台账，均为本 change 未触碰的文件，来自工作区中并行进行的其他未提交改动，与本 change 无关。
