## 1. 实施准备与依赖证据

- [x] 1.1 从确认的 rdai 提交建立隔离实施分支，记录基线 SHA 与现有失败；不混入当前工作区的在途变更。
- [x] 1.2 核对身份传输、租户绑定、任务授权、硬配额、审计切片的真实验收证据；缺失证据的依赖保持关闭。
- [x] 1.3 按 [implementation.md](implementation.md) 第 2 节固定接口契约，建立 evidence/acceptance.md 与 evidence/source-map.md。

## 2. P1 动作投影与请求门禁

- [x] 2.1 添加八个独立 capability slice、feature_action_availability 与 /auth/context.feature_actions；初始保持关闭，保留原 scheduler 五动作。
- [x] 2.2 实现启动时 RDAI_DISABLED_ACTIONS 的只关闭语义和未知键拒绝；路由与投影使用同一注册表。
- [x] 2.3 实现 Web functional-capabilities 和现有授权刷新/失效链路接入，旧租户延迟响应不得恢复状态。
- [x] 2.4 实现 test_feature_action_projection.py 与 test_feature_action_clients.cjs，验证旧服务器、缺字段、逐项关闭及无后台请求。

## 3. P2 上下文控制

- [x] 3.1 新增 fork context.py，完成 Agent/tenant/owner/session 精确匹配；补默认 Agent 存储键及同名会话回归。
- [x] 3.2 实现用量 GET 与压缩 POST、fork 导出和具体路由优先级；保留真实 HTTP 错误状态。
- [x] 3.3 修改 Agent.compact_context 的深拷贝/提交比较，成功提交后才写长期记忆；实现活动生成冲突。
- [x] 3.4 实现 test_session_context_scope.py 和 test_context_compaction_concurrency.py，覆盖非 owner、跨租户、非法 Origin、新消息、双压缩和死锁。
- [x] 3.5 新增 Web functional-context 并接现有会话生命周期。
- [ ] 3.6 实现 Web UI 测试和真实会话验收，替换这两项旧缺口断言；证据通过后才开放 usage/compact。
      真实验收已执行（`evidence/acceptance.md` §13，30 项 25 PASS / 5 FAIL 跨三个缺陷）：
      本 change 引入的 DEF-1（Web 输入框新建的会话被自己的 owner 判 404）已修（`cca39b28`，
      单元级两条新用例在撤掉修复后失败）；DEF-2 的语义已在规范中写明（`2148573e`），
      验收口径改为断言「压缩不丢正文 + 重启后正文完整」；DEF-3 是预存在的响应形状，
      客户端已按其契约把 200+`status:error` 当失败。**修复后在 `2148573e` 上的复测尚未回到全绿，
      故本项与其后的 4.5 保持未勾选**，usage/compact 继续关闭。

## 4. P3 模型编辑与 R1 验收

- [x] 4.1 新增 functional-models，完成回退链添加/删除/排序/启停与无损保存。
- [x] 4.2 实现目录 seed/overrides/hidden 草稿、隐藏/恢复/默认恢复；保存不得冻结未编辑预设。
- [x] 4.3 接 openChatFallbackModal 和 provider 卡片，登记静态资产，补三语言文案与平台权限入口。
- [x] 4.4 实现 test_functional_models.cjs，复跑既有模型 API/回退链/搜索 provider 测试。
- [ ] 4.5 完成 R1 真实验收、旧版兼容、关闭重启与回退演练；记录证据后交付 R1，R2 保持关闭。
      已完成的部分（`evidence/acceptance.md` §13）：真实部署上的 4.5.1–4.5.10 复测；
      旧版兼容与关闭重启/回退演练已在 §10 / §11 的真实演练中覆盖（关闭态 503 + 数据零改动，
      代码回退到基线二进制后既有数据逐项不变）；模型目录/回退链的真实读写与跨重启持久性为 PASS。
      未完成：4.5.4a/b 的拒绝口径（改为「被拒 + 无写入 + 客户端视为失败」，见 §13.3）与
      DEF-1 修复后的整批复测，**未回到全绿前本项不勾选**，R1 两个动作继续 `open={}` / `accepted=False`。

## 5. P4 调度目标与创建

- [x] 5.1 新增 SchedulerTargetService，复用实例授权范围与绑定；RecipientStore.list 增加 instance_ids 过滤并兼容无参调用。
- [x] 5.2 实现实例与接收者接口、白名单投影和受限 recipient_count，越权显式目标返回404。
- [x] 5.3 实现 personal 创建接口，可信目录补全目标元数据，调用 TaskAccessService.create_task；保留配额、owner、审计和执行重验。
- [x] 5.4 实现 test_scheduler_create_scope.py，覆盖跨租户/他人实例、目标伪造、无 Agent use、配额、非法 Origin 与重绑定。
- [x] 5.5 新增 Web 创建界面；防重复点击，网络结果不明时不自动重试写请求。

## 6. P5 执行归属与数据访问

- [x] 6.1 新增 run_repository.py 和幂等扩展表/索引，定义 RunScope、RunGrant、RunQuery；不回填无证据历史。
- [x] 6.2 在 _record_scheduler_run 写入可信执行归属，统一新增 runs 的业务 Agent ID；写入失败仍可 finish 且不重投。
- [x] 6.3 实现不可改写的 record、授权 SQL 查询及分页前范围限制；空 Agent 参数只聚合授权范围。
- [x] 6.4 新增 RunAccessService，复用 TaskAccessService.decide 的 owner/public 语义，逐请求重验身份和绑定。
- [x] 6.5 实现事务删除与 running 冲突，删除两表元数据且保留 messages，补审计。
- [x] 6.6 实现 test_scheduler_run_scope.py，用真实 SQLite 验证分页、默认 Agent、改绑、任务删除、历史隔离、故障和事务恢复。
- [x] 6.7 在历史功能仍关闭时执行真实测试任务，核对新增 run/scope；记录建表重复执行和旧版本忽略扩展表证据。
      → 部署持六个 deny 键（历史三路投影 `disabled_by_deployment`、路由 503）时，
      `POST /api/scheduler/run` 仍 200 并写出第三条归属行（run 与 task 逐字对应），
      历史路由保持 503；建表重复执行（首读物化、二读 200 不增行）见 `evidence/acceptance.md` §7.0a / §12，
      旧版本忽略扩展表的证据见 §11 的 `base` 阶段。

## 7. P6 调度历史与客户端

- [x] 7.1 实现运行列表、详情、删除 fork handler 和路由；history_scope=attributed_only，错误不得伪装为空列表。
- [x] 7.2 实现包含 tenant/owner/存储 Agent/session/run_id 的正文精确关联；无会话权限或无关联只返回预览。
- [x] 7.3 完成 Web functional-scheduler 的历史/详情/删除及静态资产/i18n 接入。
- [x] 7.4 实现 test_scheduler_run_http.py、test_functional_scheduler.cjs，复跑现有调度/会话测试。

## 8. P7 发布门禁、证据与交付

- [x] 8.1 按当前规范裁定已知外部连接权限失败，真实授权缺陷必须修复；独立处理旧前端/菜单/source-manifest 基线问题并保留未开放渠道证据。
- [x] 8.2 仅替换本 change 已交付八接口的缺口断言；保留未交付更新接口关闭检查，验证 fork 来源和 api/core 边界。
- [x] 8.3 完成 R2 真实渠道创建/执行/历史/删除，以及共享 Agent 双用户、跨租户、管理员和权限撤销验收。
      → 真实部署 32/32 PASS（`evidence/acceptance.md` §8；原始报文 `/tmp/rdai-acc/R2-EVIDENCE.md`）。
      本轮发现两项缺陷：详情扣留正文回 404+success **已修**（`5f6d1897`）；空 `agent_id` 的裸 Python 错误
      经基线复现判定为**预存在**，登记独立跟进。未验证项：真实渠道投递落地（缺凭据）。
- [x] 8.4 在干净隔离检出运行完整回归并记录计数/失败裁定；新增能力失败或授权失败阻断发布。
      → `74be49dd` 干净检出：27 failed / 5899 passed / 31 skipped / 416 subtests passed
      （RC 冻结 `5f6d1897` 为 5896 passed，差值即 8.5 新增的三条关停开关用例，失败数未变）。
      27 项全部取下基线对照（`f5d7d764` 同五文件 26 failed / 88 passed，失败集合逐项一致；
      scene source-hash 在新鲜检出同样 SUBFAILED），**新增能力失败 0、授权失败 0**，不阻断发布
      （`evidence/acceptance.md` §9）。
- [x] 8.5 核对 implemented/accepted/open 与证据，分批移除部署 deny 键并重启；验证前端投影与路由一致。
      → 第一批（R2 六动作）已按真实验收证据翻转为 `accepted=True` + `open`；R1 两动作保持关闭，
      待 3.6 / 4.5 的真实会话验收后再翻。真实服务两阶段演练（持键关闭 → 移除重启）投影与门禁
      **8/8 一致**；deny 键错拼拒启动；客户端资产 200 且一致性套件 11 passed；
      `close_capability_actions` 把「关停只关目标」钉进回归（`evidence/acceptance.md` §10）。
- [x] 8.6 演练先关闭再代码回退，验证扩展表保留、原任务/会话/模型配置可用；补齐版本与数据恢复记录。
      → open / closed / base(`f5d7d764` 代码回退) / restored 四阶段实测：扩展表
      `fork_scheduler_run_scopes` 与任务、会话（含消息体）、模型目录、接收者、渠道实例
      **四阶段逐项不变**；旧二进制对扩展表无感知且正常服务同一数据；恢复口径与三段版本
      （`f5d7d764` / `5f6d1897` / `74be49dd`）见 `evidence/acceptance.md` §11。
- [ ] 8.7 更新 source-map 和后续前端迁移记录，运行 OpenSpec 严格校验；所有任务和真实门槛完成后再提出归档。
