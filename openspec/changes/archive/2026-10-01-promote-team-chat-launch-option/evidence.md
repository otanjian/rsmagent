# 实施与验收证据

变更：`promote-team-chat-launch-option`（「多智能体对话」成为「新建对话」折叠菜单的第一项）

## 接入点核实

```
rg "new-chat-section|new-chat-team|new_team_chat" channel/web
```

- 实际渲染入口只有一处：`channel/web/static/js/console.js` 的 `paintNewChatMenu`，被工作台侧栏「新建对话」与历史页「新对话」两个控件共用（`NEW_CHAT_SURFACES`）。
- `channel/web/static/js/chat/new-chat.js:20-25` 有同菜单的旧副本，但 `channel/web/chat.html:3224` 只加载 `assets/js/console.js`；该文件不在加载链中，未修改。
- `desktop/src/renderer/src/components/NewChatMenu.tsx` 是独立客户端，未在本次范围内。

改动（`paintNewChatMenu`）：团队行先输出，`.new-chat-sep` 随团队行下移，单人列表作为 `.new-chat-section` 随后。未新增类名、节点、CSS、i18n 键或 feature flag。

```
menu.innerHTML = `
    ${teamRow}<div class="new-chat-section">${rows}</div>`;
```

## 自动化结果

| 命令 | 结果 |
| --- | --- |
| `node --test tests/test_agent_chat_launch_frontend.cjs` | 17 pass / 0 fail（新增 1 项「the team entry leads the picker, ahead of the solo roster」） |
| `node --test tests/test_workbench_sidebar_launch_frontend.cjs` | 21 pass / 0 fail |
| `node --test tests/test_team_chat_launch_frontend.cjs` | 21 pass / 0 fail |
| `NODE_PATH=$(npm root -g) node tests/test_sidebar_launch_browser.cjs` | 6 scenarios / 62 checks 全绿 / 0 page errors |
| `openspec validate promote-team-chat-launch-option --strict` | Change is valid |

新增的前端顺序断言（`tests/test_agent_chat_launch_frontend.cjs`）比较 DOM 字符串中 `new-chat-team` 与 `startSoloChat(` 的位置，并确认 `new-chat-sep` 落在团队行之后、单人区块之前；既有「两个入口 `innerHTML` 相同」的断言继续生效，因此顺序只有一份实现。

真实浏览器新增断言（`tests/test_sidebar_launch_browser.cjs`）读取生产页面渲染后的节点顺序：

```json
{
  "name": "the team entry leads the picker, above the solo roster",
  "ok": true,
  "rowOrder": {
    "first": "new-chat-item new-chat-team",
    "firstText": "多智能体对话",
    "trailingTeamRows": 0,
    "sepBeforeFirstSolo": true
  }
}
```

证据目录：`evidence/sidebar-launch-browser/`（含 1440×900 默认视口、1024px、360px 及 business/slate/classic 明暗配色截图）。

## 兼容与回退

- 发布开关关闭场景在浏览器用例中通过：`classic sidebar keeps its own picker shut` 与 `the history page still offers the team entry when the switch is off` 均为 `ok: true`——侧栏无折叠菜单时，历史页入口仍按新顺序提供团队入口。
- 无数据或配置迁移；无新增接口、权限门禁或 feature flag。回退方式为恢复上一版静态资源。
- 团队候选规则（coding 排除、至少两位、默认响应者）未改动，仍由原有用例覆盖（`test_team_chat_launch_frontend.cjs`、浏览器用例 `the whole picker text is free of the coding Agent`）。

## 未完成 / 边界

- 未在 Desktop 客户端同步该顺序（独立客户端，未纳入本 change）。若需要跨客户端一致，另开 change。
- 未合并 `console.css` 与 `sessions.css` 中重复的 `.new-chat-*` 样式定义；顺序改动不依赖样式，该去重留待独立 change。
