## Context

菜单由已归档 change `refine-sidebar-team-chat-launch` 建立：`NEW_CHAT_SURFACES` 描述侧栏与历史页两个「新建对话」控件，两者共用 `paintNewChatMenu`，因此选项集合、顺序与失败处理只有一份实现。当前实现把团队行拼在单人列表之后：

- `channel/web/static/js/console.js` 的 `paintNewChatMenu`：`menu.innerHTML = \`<div class="new-chat-section">${rows}</div>${teamRow}\``，其中 `teamRow` 自带 `.new-chat-sep` 分隔线。
- 行样式在两份 CSS 中重复定义（`console.css:1373` 与 `sessions.css:108` 的 `.new-chat-section` / `.new-chat-item` / `.new-chat-sep` / `.new-chat-team-ico`），顺序由 DOM 顺序决定，与样式无关。
- 团队行按类名 `.new-chat-team` 被浏览器测试点击（`tests/test_sidebar_launch_browser.cjs:306`、`:353`、`:445`），不依赖它是第几项。
- `channel/web/static/js/chat/new-chat.js` 有同一菜单的旧副本，但 `chat.html` 只加载 `assets/js/console.js`（`chat.html:3224`），该文件不在加载链中，不随本 change 修改；`desktop/src/renderer/src/components/NewChatMenu.tsx` 属于独立客户端，同样不在本 change 范围。

## Goals / Non-Goals

**Goals:**

- 「多智能体对话」在两个入口的折叠菜单中都固定为第一项，且无需滚动即可看到。
- 顺序只有一处实现，侧栏与历史页不会各自漂移。
- 分隔线仍把团队行与单人列表分开，只随团队行上移。
- 行高、配色、主题变量、键盘可达性与发布开关回退行为不变。

**Non-Goals:**

- 不改变主按钮行为、折叠箭头出现条件（可用智能体多于一位）与团队候选人规则。
- 不改变团队创建事务、权限门禁、移动端弹层与 coding 智能体提示。
- 不新增排序配置项、用户偏好或 feature flag；不把团队入口提升为独立按钮。
- 不改动 `.new-chat-item` 的视觉样式与两份 CSS 的既有重复结构（不在本 change 内合并样式文件）。
- 不改动 Desktop 客户端的 `NewChatMenu.tsx`，也不改动不在加载链中的 `static/js/chat/new-chat.js` 旧副本。

## Decisions

**D1：只调整 `paintNewChatMenu` 的拼接顺序，不改结构或类名。**
把 `teamRow` 移到前面、单人列表放进其后：

```js
menu.innerHTML = `${teamRow}<div class="new-chat-section">${rows}</div>`;
```

`teamRow` 内部仍是「团队按钮 + `.new-chat-sep`」，因此分隔线自然落在团队行与单人列表之间，两个入口同时生效。

替代方案：给菜单加排序参数或用户偏好。被否——顺序是产品语义而非用户设置，引入配置会让两个入口可能不一致。

替代方案：在 CSS 中用 `order` 反转。被否——`order` 只影响视觉顺序，DOM 与键盘 Tab 顺序会与视觉分离，无障碍上等于"看到第一项却在最后才能 Tab 到"。

**D2：分隔线归属团队行，不新增节点。**
`.new-chat-sep` 的 1px 边框与 margin 已有主题变量覆盖（`appearance.css:599`），移动端 `.history-actions .new-chat-item` 的 44px 最小高度规则（`console.css:1898`）仍作用于所有行。不新增 CSS。

**D3：以 DOM 顺序做验收断言。**
`tests/test_agent_chat_launch_frontend.cjs` 已断言两个入口 `innerHTML` 相同；新增断言团队行在单人行之前（比较 `indexOf`），并断言分隔线位于团队行之后。避免用"第一项是团队"这类只看文本位置的脆弱断言，改为对渲染后节点顺序做检查。

## Risks / Trade-offs

- [肌肉记忆与既有文档] 用户可能按旧顺序找团队入口 → 已核查 `webhelp` 与 `docs`：仅 `webhelp/lang/zh.json` 提到"在菜单里选「多智能体对话」"，无顺序描述，无需改文案；顺序变化在首次使用即自解释。
- [团队入口突出后误触] 第一项被误点会打开团队弹窗而非直接开始对话 → 弹窗可 Escape/取消且不改变原会话，风险与团队入口在末位时相同。
- [两份 CSS 重复] 顺序改动不触及样式，重复定义的风险不因本 change 放大 → 样式合并留待独立 change。

## Migration Plan

无数据或配置迁移。改动随 Web 静态资源发布即生效；回退即恢复上一版静态资源。`workbench-sidebar-launch` 的发布开关语义不变：开关关闭时侧栏无折叠菜单，历史页入口仍按新顺序展示团队入口。

## Open Questions

无。
