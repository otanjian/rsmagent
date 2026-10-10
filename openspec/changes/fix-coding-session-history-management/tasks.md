## 1. 定位与复现

- [x] 1.1 检查实际页面菜单、归档／删除请求的目标 ID 与 Agent、响应、刷新、fork 装配及资源版本，定位归档失败；暂时无法访问目标部署时明确记录待确认项，不阻断独立修复，也不宣称线上问题已解决。
      → 本机已确认代码路径与静态资源交付（见 `evidence/local-apply.md`）；目标部署归档请求的实际失败层未采集，记为待确认，不宣称线上已解决。
- [x] 1.2 将已复现的“删除最后一条当前 coding 会话后自动新建”加入前端回归用例,确认与普通会话、现有归档及删除测试的基线差异。
      → `tests/test_session_delete_coding_frontend.cjs`（8 项，含最后一条删除、相邻接续、取消保护、失败保留、迟到应答、普通会话兼容、在途去重、发送拒绝）。

## 2. 最小修复

- [x] 2.1 按定位结果修复归档链路，沿用本地归档、既有提示和恢复入口，验证当前会话／任务保留及所属 Agent 正确；不扩建归档弹窗。
      → `archiveSidebarSession`／`restoreArchivedSession` 补在途去重、忙碌标记与失败正文兜底；恢复只在原弹窗仍打开时重绘。
- [x] 2.2 分离 coding 删除后的空态和显式新建，复用现有离开／清理逻辑；覆盖删除前取消保护、相邻会话、无相邻会话和页面重载，普通会话行为保持不变。
      → `enterCodingEmptyState` / `renderCodingEmptyState` 复用 `leave({skipConfirm:true})`；普通会话仍走 `newChat(false)`。
- [x] 2.3 在现有归档／恢复／删除函数内补齐必要的等待、防重复、HTTP／业务错误及身份／目标检查；用延迟响应验证在途切换和旧列表回跳，仅修复实际缺口，不重构全部状态管理。
      → 复用 `_sessionListContext`、`_sessionActionsInFlight`、`_sessionMarkBusy`，不新建通用状态机。
- [x] 2.4 删除分流严格区分编码关联读取异常与无关联，异常及已有 coding 记录缺关联时保留记录并报错；保持普通删除、远端调用顺序、幂等和文件保留契约，不新增停止确认轮询或生命周期协议。
      → `_coding_link_for_delete` + `link_for_delete` + `session_exists`；`500 coding_link_unavailable` / `409 coding_not_linked`。

## 3. 验证与说明

- [x] 3.1 运行相关前后端回归，补测最后一条 coding 删除不新建、删除失败保留、归档／恢复、在途换会话／身份和关联读取异常；复用既有权限、跨 Agent、幂等及文件保留用例，不重复建设测试矩阵。
      → 后端 96 + 31 通过，前端 149 通过；新增用例见 `evidence/local-apply.md`。
- [ ] 3.2 核对修改所依赖的现有装配、权限和客户端代码后，在可用环境用实际页面和配套 OpenCode 验证自有会话的归档→恢复→删除，并覆盖 `opencode.enabled` 关闭／服务离线及普通会话回归；记录环境和限制，生产确认单独如实标记，不把替身测试等同真实服务通过。
      → 本机未登录真实账号、配套 OpenCode 不可达，端到端未完成，记为待确认（见 `evidence/local-apply.md` 文末）。
- [x] 3.3 更新 `docs/session-history-panel.md`，核对必要文案三语及原控件可达性；确认无数据迁移、旧会话／归档值兼容和资源版本戳，说明代码回退不回滚数据，执行严格 OpenSpec 校验并记录结果。
      → 文档已补“编码会话的归档、恢复与删除”一节；i18n 三语已补；`openspec validate --strict` valid。
