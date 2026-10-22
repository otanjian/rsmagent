# 前端适配评审：`views/sessions.js`

## 结论

本 change 在参考模块 `channel/web/static/js/views/sessions.js` 上做了一处**受控适配**，并已在 `docs/design/upstream-sync/capabilities.json` 的 `source_reviews` 中登记对应的 `integration`（源 blob、候选 sha256、消费者、测试与证据）。

## 为什么必须改参考模块

历史列表的取数 URL 与行渲染都在 `views/sessions.js` 内：

- `_fetchSessionPage` 里硬编码 `scope=all`，跨用户范围需要在同一函数内选择 `read_scope=agent_all&agent_id=...`；
- `_sessionItemEl` 负责行的点击行为、更多菜单与元信息，只读记录需要在同一处改为打开只读详情、隐藏写操作并显示创建人。

这两处没有现成的 fork seam；在 fork 模块里重写整段函数会复制上游实现，反而更难同步。因此按仓库既有的 `source_reviews` 机制做最小、可复核的适配。

## 改动范围

- 只新增/替换与该功能直接相关的行：范围状态与请求 URL、行元信息、只读点击与菜单隐藏；
- 其余 fork 逻辑（目标加载、只读弹层、报告生成/下载、i18n、CSS）在 fork 模块中：
  - `channel/web/static/js/fork/views/sessions.js`
  - `channel/web/templates/fork/views/business.html`
  - `channel/web/static/css/fork/extensions.css`
  - `channel/web/static/js/i18n/core.js`
- 未改 `channel/web/api/**`、`core/**`、`chat/**` 下的参考模块。

## 消费者

- `channel/web/chat.html`
- `channel/web/static/js/fork/views/sessions.js`
- `channel/web/templates/fork/views/business.html`

## 测试

- `tests/test_history_read_scope_frontend.cjs`
- `tests/test_enterprise_interview_acceptance.py`

## 后续同步注意

上游下次更新 `views/sessions.js` 时，需要重新复核这两处适配并更新 `candidate_sha256`；范围/报告的主体逻辑仍在 fork 模块，重放成本受控。
