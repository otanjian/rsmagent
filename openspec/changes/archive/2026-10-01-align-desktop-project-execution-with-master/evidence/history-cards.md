# 按生成时来源重建历史卡片（任务 9.6，验收 A23/A24/A34）

对应主规范 `desktop-project-artifacts` 的「历史重建」——原文：*按生成时来源重建历史，
处理切目录、版本变化、删除、离线及跨设备访问；不自动上传/迁移文件，不把最近候选当
授权。*任务原文：*按生成时来源重建历史。*

同组里 9.1–9.5 交付的是「**实时**产出怎么变成一张卡片」，本文只记录那条记录的
**第二次**出现：用户重开一段对话时，卡片从 `steps` 里重建出来。这不是同一件事。

## 一、要回答的问题

一张从历史里重建出来的卡片，属于**哪个项目**？

三个答案都"差不多能跑"，但只有一个是诚实的：

| 做法 | 切过目录以后 |
|---|---|
| 用会话**现在**的 `execution_target` | 卡片挂着新项目的 workspace，相对路径按新项目解析 —— 若新项目里恰好有同名文件，**打开的是另一个文件**，而且看不出来 |
| 用「最近使用」的目录 | 撤权的目录会"复活"：最近候选不是授权 |
| 用**当下仍注册、真的持有这个绝对路径**的项目 | 卡片保留生成时的 device/workspace/binding 与相对路径；不在盘上就是"已删除" |

本任务取第三种。

## 二、交付内容

### 2.1 判定只有一处：谁是持有者（设备端）

`agent/desktop_local/__init__.py::LocalRootRegistry.entry_for_path(path, user_id=, tenant_id=)`
回答"这个绝对路径现在归哪个注册"，四条性质各自都曾是别处的 bug：

| 性质 | 为什么 | 如果不做 |
|---|---|---|
| **只认仍然在册的注册** | 撤权、重选目录、断连都要如实回答"没有" | 「最近使用」变成事实上的授权，撤权后卡片还能打开 |
| **同一 user + tenant** | 同一台机器上另一个账号的项目不是这个调用者的 | 跨账号读到别人的目录 |
| **最内层优先** | 嵌套项目都"持有"这个路径 | 内层项目的产出被记到外层项目的 workspace 与相对路径上 |
| **两侧 realpath，且前缀比对带分隔符** | 项目里放一个指向项目外的链接就能把任何路径伪装成成员；`/p/project-ab` 也不是 `/p/project` 里的文件 | 卡片指着一个本项目并不持有的文件 |

`None` 是**答案**不是错误：读不出就当作"这里没有这个项目"，绝不退回按 cwd 猜。

### 2.2 卡片保留生成时的身份（服务器端）

`channel/web/fork/runtime.py`：

- `_project_holding(path, identity)` —— 按**当前请求身份**查实时注册，只问一句
  "谁持有它"；
- `_origin_for_entry(entry, step)` —— 用**注册**（不是会话目标）造 `desktop_origin`：
  `device_id` / `workspace_id` / `binding_id` / `grant_version` / `project_mode` 全部
  来自那次授权，`run_id` / `tool_call_id` 来自那一步;
- `_artifacts_from_steps` —— 每个记录下来的绝对路径各自问一次，`step_root` 随持有者
  走;命中一个**不持有**该路径的注册时返回 `None`，于是它最多被当成服务器文件，
  **永不**被声称为本机产出（另一台机器的路径就是这样被拒绝的）。

### 2.3 文件不在了：记录留下，且说明它不在了

实时产出与历史记录是**两种主张**：

- 实时（9.1）：「这个文件现在可以打开」—— 文件没了就不发卡片，因为卡片是承诺；
- 历史（9.6）：「那次运行产出了这个文件」—— 丢掉卡片等于删掉这次工作唯一的痕迹，
  而改成服务器引用又是指向一个本进程从未拥有过的文件。

`_missing_local_card` 因此渲染一张 `resolution: "missing"` 的卡片：相对路径、名字、
类型照旧（都由注册与路径推得），`size`/`source_version` 留空 —— 读不到就不声称版本。
`_local_artifact_payload` 的 `resolution` 字段随卡片一路走到前端。

### 2.4 面板：不属于当前项目就拒绝，而不是重新解析

`channel/web/static/js/fork/project-source.js::wrongProject(expectedWorkspaceId)`：
动作与预览都要先过这一关。**实时**卡片从不带 `expectedWorkspaceId`（它的路径就在当前
项目里，当前项目的 workspace 就是对的），**重放**卡片带着自己那份；两者不相等即
`wrong_project`，**不发出**任何宿主调用 —— 发出去的相对路径会被按当前项目解析，那正是
"静默打开另一个项目里的同名文件"。

`channel/web/static/js/workspace.js`：卡片负载补 `workspace_id` 与 `resolution`（前者
由 `wsLocalCardScope` 转成 `expectedWorkspaceId`，对实时卡片为空对象；后者由
`wsLocalCardIsGone` 判）；`not_found` 与 `wrong_project` 各有一句自己的话
（`ws_local_file_gone` / `ws_local_other_project`，三种语言）。

### 2.5 不自动上传 / 不迁移

历史卡片是 `source: "desktop"` 的引用，**没有** `raw_url` / `preview_url` / `abs_path`，
也没有任何"把文件放到服务器上再给链接"的分支。用例直接断言服务器工作区目录**仍然为空**。

## 三、验证

| 层 | 套件 / 脚本 | 结果 |
|---|---|---|
| 注册表 | `tests/test_desktop_local_root.py::LocalRootLookupByPathTests` | 10 项：最内层优先、根自身算持有、项目外/兄弟前缀、跨用户、跨租户、撤权不复活、畸形路径、链接逃出、链接留在项目内 |
| 历史重建 | `tests/test_desktop_artifact_source.py::HistoryTests` | 4 项新增：卡片认生成时的项目而不是现在打开的那个、撤权不从"最近"回来、删除后卡片仍在且 `resolution: missing`、历史卡片永不上传（服务器目录为空） |
| 适配层 + 页面 | `tests/test_desktop_project_native_actions.cjs` | 8 项新增：act/preview 对另一个项目 refuse 且**不发出**调用、同项目照常、无主卡片保持原行为、面板对 `missing` 直接 `not_found`、卡片把项目带给适配层、`wrong_project` 有自己的文案、卡片负载带 `workspace_id`/`resolution` |
| 变异 | `evidence/scripts/mutate_history_cards.py` | **16 类走样 16/16 命中**（日志 `evidence/history-cards-mutations.log`） |

变异覆盖：不查实时注册（只认会话目录）、身份取"现在打开的项目"、文件不在就丢卡片、
不带 `resolution`、外层项目赢、前缀不加分隔符、不按用户/租户过滤、不做 realpath、
面板不传项目、面板不判 `missing`（动作与预览各一）、卡片不带项目、`wrong_project`
没有独立文案（动作与预览各一）、适配层 act/preview 各丢一次守卫。

`mutate_history_cards.py` 已纳入 `tests/test_mutation_evidence_scripts.py` 的结构守卫
（锚点存在、唯一、只改声明的源文件、必带预期失败）。

## 四、坑（写下来而不是记住）

1. **同名锚点会静默改错地方**。`if (wsLocalCardIsGone(meta)) return …` 与
   `if (code === 'wrong_project') return …` 在 `workspace.js` 里各出现**两次**
   （动作与预览各一份），`str.replace(old, new, 1)` 只改第一处。首次跑 H10/H12 因此报
   "0 failed"，看着像用例太弱，其实是变异打偏了。两处锚点都已延长到各自独有的邻居行；
   `test_every_anchor_is_the_only_place_it_could_mean` 就是为这类事故加的。
2. **macOS 上 `/var` 是 `/private/var` 的符号链接**：只把**一侧**的 `realpath` 换成
   `normpath` 会被这个系统链接掩盖，链接逃逸用例照样"通过"（假绿）。H8 因此改成
   两侧都退化为字符串比对的写法 —— 要测的是"必须 realpath"，不是"某一行写了 realpath"。
3. **`resolution: 'missing'` 必须两端都有**：服务器不带这个字段，前端就没有判据；
   前端不判，卡片仍然会被按当前项目解析。所以 H4（服务器）与 H10/H10b（前端）分开测，
   任何一端被拿掉都各有用例变红。

## 五、边界与未做

- **真实的"离线设备"未做端到端验证**：注册表里断连即 `revoke`（用例
  `test_a_revoked_root_is_not_resurrected` 覆盖同一后果），但"设备在线、网络抖动"这一
  情形属 7.x 的故障注入与 9.8 的 E2E，本文不声称验证过。
- **版本变化**只做到"卡片带 `source_version` + `revalidate: true`，被读到时按当前内容
  重新校验"；"同一路径换了个文件"的 UI 呈现（对比版本）不在本任务，属 9.8 验收。
- 安装件真机属 10.1–10.3，Windows 未验证。
