# 搜索与标签筛选验收

日期：2026-09-23。实现与规范完成，未归档；未提交其他进行中的工作区改动。

## 实现

- 原工作台标题下增加关键词搜索、单选标签与动态数量、未打标签、清除条件、无匹配状态。
- 完整名单仍供聊天选择器使用；默认排序、可运行状态、旧请求隔离及失败重试保持原路径。
- 账号退出/切换与租户切换清除旧状态；刷新保留条件，已选标签消失则回到全部。
- 管理表单支持英文/中文逗号，去空白、空项及重复项；租户投影补齐 position/category/tags。
- 标签默认一行，展开/收起按钮固定于第一行右侧，展开后限高滚动，选中标签靠前；卡片网格按内容计算行高，避免标签导致底部动作溢出。
- 简中、繁中、英文文案及 i18n 快照已更新。

## 自动回归

Python：161 passed。

```sh
.venv/bin/python -m pytest tests/test_agent_workbench.py tests/test_private_agent_owner_reachability.py tests/test_tenant_default_agent.py tests/test_agent_registry.py tests/test_agent_admin.py tests/test_coding_agent_type_boundary.py -q
```

Node：120 passed。

```sh
node --test tests/test_agent_workbench_frontend.cjs tests/test_agent_profile_frontend.cjs tests/test_console_i18n_parity.cjs tests/test_agent_chat_launch_frontend.cjs tests/test_composer_agents_frontend.cjs tests/test_team_chat_launch_frontend.cjs tests/test_tenant_default_agent_frontend.cjs tests/test_user_default_agent_frontend.cjs tests/test_workbench_menu_grant_frontend.cjs
node --check channel/web/static/js/console.js
openspec validate add-agent-workbench-tag-filter --strict
git diff --check
```

新增回归覆盖：搜索字段与大小写、交集和每智能体计数、旧响应缺少标签、刷新条件保留及标签消失、聊天目录不被收窄、中文组合输入、账号/租户和乱序响应、危险文本转义、合成筛选与同名标签、折叠标签焦点范围、中英文逗号保存。

额外检查 tests/test_sidebar_account_frontend.cjs：54 passed / 5 failed。失败涉及旧 legacy/public/unknown 身份模式预期；用只移除本 change 新增 resetAgentWorkbenchFilters 调用的源代码重新运行，同样 5 项失败，确认不由本次接入产生。未修改这些无关断言。

## 浏览器

通过 cua_repl 操作真实浏览器，使用实际 chat.html、静态 CSS/JS 与独立 9907 端口的内存测试响应；未伪造正式服务登录、未修改实际智能体或会话。可用 `node openspec/changes/add-agent-workbench-tag-filter/evidence/serve-fixture.cjs` 重现页面。

- 17 项测试名单：全部为 17，多标签分组分别计数；未打标签为 1。
- 搜索“对账”：全部为 2，财务为 1；再选财务仅有银行对账，刷新仍为 1。
- 不存在的关键词：0 项并出现无匹配提示和清除入口；清除恢复名单。
- 展开可见全部 25 个筛选选项；未打标签筛选只出现默认助理；收起后选中标签仍可见。
- 键盘 Enter 选中财务后，焦点回到选中标签，结果正确。
- 浅色、深色与 390×844 视口均可使用。窄屏展开区高 200px，卡片滚动区仍高 384px；页面 scrollWidth 与 clientWidth 同为 390px，无横向溢出。
- 修复卡片底部溢出后，同排三张卡片均高 229px，动作均在卡片内。
- 浏览器无 error，仅有既有 Tailwind CDN 使用方式警告。

截图：

- [浅色工作台](evidence/workbench-light.png)
- [深色工作台](evidence/workbench-dark.png)
- [组合筛选](evidence/workbench-filtered.png)
- [窄屏长标签](evidence/workbench-mobile.png)

## 发布与验证范围

首次交付时 9899 服务尚未重启，旧 Python 进程没有载入新增的标签投影字段，导致新页面把全部 69 个智能体归为“未打标签”。2026-09-23 根据用户反馈完成运行态修复：向旧进程 PID 55373 发送 SIGTERM，正常退出后用相同解释器、入口及工作目录启动 PID 97341，保留追加日志，并同步本地 PID 文件。HTTP 健康检查返回 200。

使用用户已登录的 Chrome 工作台（AI租户1）完成真实数据验证：

- 全部智能体为 69；“未打标签”为 1，仅包含 SAP 智能助手。
- “经营分析参谋”的经营分析、归因、指标体系、经营决策四个标签均正确显示。
- 点击“经营分析 1”后显示“找到 1 个智能体”，结果为 business-analysis-test15。
- 验证后恢复“全部 69”并收起标签。未修改任何智能体标签数据。

真实筛选截图：[经营分析标签筛选](evidence/workbench-live-filtered.png)。静态资源由现有模板按 mtime 更新缓存版本；后端 Python 改动需要重启服务才能生效。

无需数据库迁移、标签批量改写或新增 feature flag。标签唯一来源仍是原配置数组；旧客户端忽略新增展示字段，新前端面对旧响应将缺少标签视为未打标签。回滚时仅撤回本 change 的增量，保存过的数组仍兼容旧版本。

## 单行标签布局调整（2026-09-23）

按用户反馈将默认折叠从两行调整为一行，展开/收起按钮放入同一横向容器的右侧；测量折叠标签时先预留按钮宽度，超出第一行的标签使用 hidden 移出键盘焦点序列。窄屏时为选中长标签预留“全部”按钮空间，文字省略但保留完整可访问名称。

- 前端定向回归 36 passed；console.js 语法检查通过。
- 已登录 Chrome 的真实 9899 工作台，默认可见标签只有一个 offsetTop，标签区域高 36px；展开后所有标签可见，收起按钮仍与第一行对齐。
- 390×844 窄屏选择“逾期应收账龄分档与差异化催收方案”后收起，选中标签仍在第一行，展开按钮与标签顶部同为 204px；页面 clientWidth/scrollWidth 均为 390px。
- 验证后恢复桌面原视口及“全部 69”的收起状态。本次仅修改静态页面与前端逻辑，无需重启后端。

截图：[桌面单行](evidence/workbench-single-row.png)、[窄屏长标签](evidence/workbench-single-row-mobile.png)。
