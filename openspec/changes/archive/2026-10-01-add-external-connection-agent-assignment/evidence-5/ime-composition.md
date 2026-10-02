# 搜索输入的中文输入法（IME 组合）

现场问题：在分配弹窗的「按名称或 ID 搜索」框里**打不出中文**（截图见 `../assets` 的现场记录，弹窗搜索框被标注）。

## 1. 根因

中文输入法是两段的:先在输入框里写**预编辑文本**（拼音），提交后才产生字符。原有实现两处都不兼容：

| 行为 | 后果 |
| --- | --- |
| `input` 事件（含 `isComposing=true` 的预编辑事件）直接当成查询 | 按拼音去搜索、按下拼音筛选，每个音节都重绘一次 |
| 每次搜索/筛选都 `innerHTML = …` 重建面板与页面 | 输入法正在书写的那个 input 节点被替换，**未提交的拼音随之丢失**，浏览器连 `compositionend` 都不再送出 —— 表现就是「打不出中文」 |

触发条件很宽：预编辑文本停留超过 300ms 防抖（挑词、停顿）就会命中，所以几乎总是发生。

## 2. 修法（`channel/web/static/js/external-connections.js`）

一个共用的组合状态，两个搜索框（目录搜索 `#ec-search` 与弹窗搜索）都遵守：

- `isComposing` 为真的 `input` 不作为查询；`compositionend` 才按提交文本查询；
- 组合期间 `ecRender()` / `ecAssignRender()` 只登记「待重绘」，不重建 DOM；提交后补做；
- 提交后浏览器补发的普通 `input` 与提交词相同，`ecAssignSearch` 按词去重，**同一关键词只请求一次**；
- 普通 `input`（`isComposing === false`）可清掉因丢失 `compositionend` 而残留的组合标记，避免输入框被永久冻结；
- 关闭弹窗时清掉组合标记（节点移除不会再触发 `compositionend`）；
- 输入法消费掉的按键（`isComposing` 或 `keyCode === 229`，浏览器就是这样标注的）不交给页面处理：组合中的 Escape 是**取消候选**，不是「关闭弹窗丢弃草稿」。

## 3. 证据

### 3.1 单元（`tests/test_external_connections_frontend.cjs`，40 pass）

- `an IME composition is not repainted away, and only the committed text searches`：组合期间 `pendingTimers()` 为 0、无候选请求、输入框节点**同一性不变**；提交后按 `q=合同` 只请求一次；补发的普通 input 不重复请求。
- `a repaint asked for during a composition is deferred, not dropped`：组合期间到达的响应请求重绘 → 推迟；提交后补做。
- `the catalogue search box survives a composition too`：目录搜索框同样不按拼音筛选、不重绘，提交后才生效。

RED 记录（实现前，同样三条用例）：

```
✖ an IME composition is not repainted away, and only the committed text searches
  AssertionError: 拼音不触发防抖搜索
✖ a repaint asked for during a composition is deferred, not dropped
  AssertionError: 组合未提交时输入框不得被重建
✖ the catalogue search box survives a composition too
  AssertionError: 组合中的拼音不作为筛选条件
ℹ pass 37 fail 3
```

### 3.2 真实浏览器（`tests/test_external_connections_browser.cjs`，12 scenarios / 0 failed）

新增三条场景用 Chromium 自己的输入法模拟（CDP `Input.imeSetComposition` 写预编辑文本、`Input.insertText` 提交、`Input.dispatchKeyEvent` 送 VK 229），事件序列与真实中文输入法一致：`compositionstart` → `input(isComposing=true, value=拼音)` → `input(isComposing=true, value=中文)` → `compositionend`。

- `中文输入法组合提交前不重建分配搜索框`：预编辑 `hetong` 停留 600ms（远超防抖）后输入框仍在（节点标记未被替换），无 `q=hetong` 请求；提交「合同」后只发一次 `q=%E5%90%88%E5%90%8C`，候选行可取；截图 `browser/assign-ime-composition.png`。
- `中文输入法组合提交前不重建目录搜索框`：同样结论，且组合期间连接卡片仍在（没有按拼音筛选），提交后按中文筛选出「没有找到匹配的连接」。
- `输入法取消候选的 Escape 不当作关闭弹窗`：组合中送 `keyCode 229 / isComposing` 的 Escape，弹窗与搜索框都还在；随后一个**非输入法的** Escape 仍然关闭弹窗（区分两者，而不是让 Escape 失效）。

把修复临时改回原行为后重跑，两条场景如实失败，失败点正是根因：

```
AssertionError: 浏览器必须真的送出输入法组合事件，否则这条用例什么也没验证：
compositionstart,compositionupdate
```

—— 日志里没有 `compositionend`：输入框在组合中被重绘掉了，提交事件永远不会到达页面。

```
AssertionError: 输入法取消候选不得关闭分配弹窗   (false !== true)
```

—— 未加按键守卫时，组合中的 Escape 直接把弹窗关掉了。

## 4. 复核

```
node --test tests/test_external_connections_frontend.cjs          → 40 pass / 0 fail
COW_EXTERNAL_BROWSER_OUTPUT=... node tests/test_external_connections_browser.cjs
                                                                 → 12 scenarios, 0 failed
NODE_PATH=$(npm root -g) .venv/bin/python -m pytest tests/test_external_connections_browser.py -q -p no:randomly
                                                                 → 1 passed
```

相关回归见 `regression.md`；需求已写入 `specs/external-connection-management/spec.md`（ADDED「搜索输入支持输入法组合」）。
