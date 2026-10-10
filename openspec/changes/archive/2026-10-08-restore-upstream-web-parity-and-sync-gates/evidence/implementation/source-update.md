# 源码更新适配实施记录

继续实施时采用部署配置加外部受信 manager 协议，不猜测当前业务部署的发布来源。配置与协议见 `docs/operations/source-update.md`。测试源为每次新建的本机裸 Git 仓库 `refs/heads/release`，manager 管理隔离 HTTP 进程，数据为既有 instance-backup 测试夹具；没有更新真实业务服务。

已经通过 12 个实际 Git/venv/HTTP 进程与 WSGI 用例：目标绑定、准备失败保全、并发及重复启动幂等、迁移后健康失败恢复、ref 前移/脏树拒绝、promote 失败不覆盖后续写入、死 worker 恢复状态、平台/CSRF/请求白名单与审计、运行实例绑定、不支持安装类型、当前 SHA/非快进拒绝和配置变更失效。所有用例实际执行，无跳过；同候选机器结果见 `required-local/pytest-2.xml`，最终指纹见 `required-local/report.json`。

浏览器菜单使用唯一的 `fork/source-update.js`，参考固定上游 `core/version.js` 的入口行为，保留 fork 品牌和账户菜单。确认绑定服务端 check_id，身份切换取消请求/轮询/旧确认；全局 fetch 对平台更新端点不再注入 Agent 字段。该问题由浏览器实际装配测试发现，后端继续严格拒绝非白名单字段。

边界：测试 manager 不是部署适配器；目前的端到端更新载荷是隔离的受控测试服务，尚不能冒充 Windows 业务实例或完整生产 Agent 的升级验收。任务 5.9 的目标部署验收继续保留，直到目标安装的进程监督、隔离及恢复链得到实证。
