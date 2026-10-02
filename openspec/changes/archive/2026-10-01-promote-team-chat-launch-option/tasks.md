## 1. 菜单顺序实现

- [x] 1.1 `paintNewChatMenu`（`channel/web/static/js/console.js`）改为先输出团队行、再输出单人列表：`menu.innerHTML = \`${teamRow}<div class="new-chat-section">${rows}</div>\``，保留 `teamRow` 自带的 `.new-chat-sep` 作为团队行与其下单人列表的分隔，不新增类名、节点或 CSS。
- [x] 1.2 核对侧栏折叠与历史页两个入口共用该绘制实现，确认没有第二处拼接菜单 HTML 的位置（`rg "new-chat-section|new-chat-team" channel/web`）需要同步修改。
  - 仅 `console.js` 在加载链中；`static/js/chat/new-chat.js` 的旧副本未被 `chat.html` 加载，不做修改。见 `evidence.md`「接入点核实」。

## 2. 自动化验收

- [x] 2.1 在 `tests/test_agent_chat_launch_frontend.cjs` 增加顺序断言：团队行索引小于任一单人行索引，且 `.new-chat-sep` 位于团队行之后；保留既有「两个入口 `innerHTML` 相同」的断言。
- [x] 2.2 运行 `node --test tests/test_agent_chat_launch_frontend.cjs`，确认新断言与既有断言全部通过。
  - 17 pass / 0 fail；另跑 `test_workbench_sidebar_launch_frontend.cjs`、`test_team_chat_launch_frontend.cjs` 各 21 pass / 0 fail。
- [x] 2.3 运行 `node --test tests/test_workbench_sidebar_launch_frontend.cjs` 与 `node --test tests/test_sidebar_launch_browser.cjs`，确认侧栏折叠菜单在真实浏览器中渲染完整（行数、可见性、高度、主题配色）且无控制台错误；如浏览器用例不可执行，记录原因并说明替代验证方式。
  - 浏览器用例 6 scenarios / 62 checks 全绿 / 0 page errors；新增「the team entry leads the picker, above the solo roster」由生产页面 DOM 顺序取证。

## 3. 兼容、回退与收尾

- [x] 3.1 确认发布开关关闭时行为不变：侧栏无折叠菜单，历史页「新对话」菜单仍按新顺序展示团队入口（`test_sidebar_launch_browser.cjs` 的开关关闭场景）。
- [x] 3.2 确认无数据/配置迁移、无新增接口与 feature flag，回退方式为恢复上一版静态资源；在 `design.md` 记录实际接入点若有偏差则同步修正。
- [x] 3.3 运行 `openspec validate promote-team-chat-launch-option --strict` 并完成一次 1440×900 与 375px 下的手工确认（团队入口在菜单首屏可见、可点击、可 Escape/取消，键盘 Tab 顺序与视觉顺序一致）。
  - `--strict` 通过。浏览器用例在 1440×900、1024px、360px 下渲染并截图，新断言证实团队入口为 DOM 首项；因顺序改动直接取自 DOM 顺序（未使用 CSS `order`），键盘 Tab 顺序与视觉顺序一致。
