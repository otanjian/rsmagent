# hide-agent-config-fields 验收证据

日期：2026-09-24。记录本次收缩控制台三处控件的红/绿核验、回归对比、服务端实际投递的产物与规范校验输出。

## 1. 规范校验

```
$ openspec validate hide-agent-config-fields --strict
Change 'hide-agent-config-fields' is valid
```

## 2. 红：新断言在改动前失败

在一个干净检出上运行（`git worktree add --detach /tmp/rsm-baseline HEAD`，HEAD = `6a8ebccd`），把新用例拷入后执行：

```
$ node --test tests/test_agent_config_fields_hidden_frontend.cjs
✖ 概况 pane hides 分类 and 关联场景 and keeps the other profile fields
  AssertionError [ERR_ASSERTION]: 分类 must not be rendered
✖ 能力 pane hides the SOP block and keeps the skills and tools blocks
  AssertionError [ERR_ASSERTION]: the SOP heading must not be rendered
✖ saving other profile fields round-trips the hidden fields unchanged
  AssertionError [ERR_ASSERTION]: the hidden 分类 keeps its stored value
      actual: ''   expected: 'procurement'
ℹ tests 3  ℹ pass 0  ℹ fail 3
```

第三条是本变更真正的理由，值得单独说明：**只要「分类」还在被渲染，`saveAgentProfile` 就会读到那个空的下拉，把用户存好的 `procurement` 覆盖成 `''`**。它不是「多一个占位控件」，而是在任何一次无关保存上主动清数据。撤掉渲染后，既有的 `catEl ? ... : agent.category || ''` 兜底才接管。

## 3. 绿：改动后

```
$ node --test tests/test_agent_config_fields_hidden_frontend.cjs
✔ 概况 pane hides 分类 and 关联场景 and keeps the other profile fields
✔ 能力 pane hides the SOP block and keeps the skills and tools blocks
✔ saving other profile fields round-trips the hidden fields unchanged
ℹ tests 3  ℹ pass 3  ℹ fail 0

$ node --test tests/test_agent_profile_frontend.cjs tests/test_agent_config_fields_hidden_frontend.cjs
ℹ tests 9  ℹ pass 9  ℹ fail 0
```

第三条用例是端到端而非桩断言：harness 只让渲染产物里出现过的元素可被 `getElementById` 找到，所以「控件仍在渲染」与「保存会把值写空」是同一个事实的两面。

## 4. 回归对比

```
$ node --test tests/*.cjs
干净检出（HEAD 6a8ebccd）：  tests 891  pass 871  fail 20
本次工作树：                tests 932  pass 922  fail 10
```

工作树的 10 条失败**全部**出现在干净基线的失败集合内（10/10），即本变更新增失败为 0。这 10 条位于本变更未触碰的文件：`test_sidebar_account_frontend.cjs`（5 条）、`test_appearance_browser.cjs`（整文件）、`test_personal_console_frontend.cjs`（整文件）、workbench 场景派发与角色模型默认各 1 条。

（干净检出比工作树多出的失败来自工作树未纳入版本管理的 `node_modules` 与其它在用改动，与本变更无关。）

## 5. 服务端实际投递的产物

不必等打包：控制台按静态路径直投 `console.js`，已确认投递内容与磁盘一致。

```
$ curl -s http://localhost:9899/assets/js/console.js -o /tmp/served_console.js   # http=200 bytes=980761
$ diff -q /tmp/served_console.js channel/web/static/js/console.js
IDENTICAL

# 投递产物中的出现次数
agent-edit-category    1     <- 仅 saveAgentProfile 的兜底读取（第 4049 行）
agent-edit-scene       1     <- 仅 saveAgentProfile 的兜底读取（第 4050 行）
agent-sops-list        0
agent-sop-add          0
agents_sops_label      0
```

两个 `agent-edit-*` 各剩 1 处，是刻意保留的：那是保存路径在控件缺失时回退到 `agent.category` / `agent.scene_id` 的入口。渲染模板里已无对应元素，因此它们在页面上恒为 `null`。

## 6. 既有用例的调整

`tests/test_agent_profile_frontend.cjs`（本变更之前未被其它改动触碰）：

- 新增 `WITHDRAWN_IDS`，令该文件的 document 桩对两个已撤除控件返回 `null`——这正是真实页面的行为，也是兜底生效的条件。
- 两个切片的锚点随 `_sceneCatalogCache`、`// Scene catalog options` 的撤除而更新；`saveAgentCapabilities` 用例原先经 `setup()` 间接依赖旧锚点，一并恢复。
- 「saveAgentProfile sends the digital-employee fields」由「断言两个下拉写入的值」改为「断言这两个字段回传 Agent 既有值」。

## 7. 文档检索

- `webhelp/scenario_docs.json`（3.4 MB 用户文档）不含「人设摘要」「关联场景」「SOP 流程」，**无需同步**。（早先一次宽泛检索曾误报命中，原因是 `职位.*分类` 这类跨行贪婪匹配落在一行超长压缩 JSON 上；逐词精确检索为空。）
- `docs/design/agent-digital-employee-config.md`（2026-09-09 的实施记录）第 59 行的历史验收步骤仍写着「在概况填写职位/分类/标签/问候/人设摘要/关联场景」。那是当时那次 change 的验收记录，按不修改历史记录的原则**保持原样、不回写**；口径差异以本 change 的 spec 为准。

## 8. 未做与未验证

- **未改服务端与数据**：`agent/admin.py`、`channel/web/fork/handlers/agents.py`、`agent/registry.py`、`agent/effective_capabilities.py`、`bridge/agent_bridge.py` 均未改动。
- **未删除任何 i18n key**，`tests/fixtures/console_i18n_snapshot.json` 未由本变更改动。
- `desktop/dist/renderer/js/console.js`（构建产物）**未同步**，桌面侧待下次打包。
- **页面实测未完成**：`/admin` 需要登录会话，浏览器内无凭据，故未逐屏确认。已用第 5 节的「投递产物 == 磁盘源码」作为等效证据（服务端无构建步骤、无缓存层）。tasks 5.1 / 5.2 的逐屏确认仍待有会话的人执行。
