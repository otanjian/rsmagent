# 账号记忆管理

控制台「记忆管理」显示登录账号在当前租户的个人记忆，切换 Agent 不改变归属。管理员使用同一入口，不获得其他成员的个人记忆权限。

- **记忆文件**：`MEMORY.md`、`memory/*.md`；用于个人记忆检索。
- **自主进化**：`memory/evolution/*.md`、`memory/dreams/*.md`；为过程记录，不自动进入检索或蒸馏输入。
- 点击条目可查看 Markdown，编辑支持 Ctrl/Cmd+S、未保存提醒和版本冲突确认。删除需要当前版本；“清空我的记忆”明确展示账号、租户及四类数量。
- 全分类清空只删除这四类个人文件和对应索引，人设、聊天历史、共享记忆、技能、进化备份不变。
- “索引待恢复”表示正文已经保存，检索尚未完成同步；用“重试索引”重建当前版本。不要重复输入相同记忆来代替恢复。
- 空列表不代表接口错误。真正无数据、权限/服务错误和索引待恢复分别显示。明确要求 Agent 记住偏好会通过 `memory_add(scope=user)` 创建可管理文件。

## 升级与部署

本次应用在 `rdai` 工作区，功能来源固定为 master `48c0d79c36146950667f8d1884ab823deae62305`，来源映射见 change 的 `evidence/implementation.md`。部署须同步 Python、HTML 模板和 JS；第一方资源由模板渲染器附加 mtime 版本。9899 服务使用 `autoreload=False`，代码修改不会替换已载入的 Python 模块；2026-10-02 已重启服务并在真实页面确认编辑/删除入口。后续更新仍须重启服务，再刷新 Web / Desktop 内嵌控制台。

服务使用每个用户根的 `.memory.lock` 跨进程锁，以及 `.memory-scope.json` 的 operation version / clear generation。不要删除、替换运行中的锁文件。macOS/POSIX 已覆盖两个独立进程更新同一状态；Windows 分支使用平台锁，但尚未做 Windows 实机验收。Windows 文件路径访问仍依赖现有操作系统 ACL，不能把应用文件目录交给不受信本地写入者。

`personal_memory_write=false` 只关闭新增/修改；本人读取、删除、清空和索引修复保留。回撤时先停止派发后台任务，关闭开关并重启写进程，等待旧进程退出后再重新开放；这样不会让关闭前排队的任务跨开关周期继续运行。已发生编辑/清空的旧版本在最终提交时也会被拒绝。不要把旧运行时和新运行时混用来写同一用户根。

正文是内容真值，索引可重建。发布批次的 `.memory-publish.json` 用于补齐仍有效的正文，不要手工删除；状态损坏返回明确错误，不按“新账号”重置版本。审计只记录操作类型、账号/租户、版本、数量和索引状态，不记录正文。审计不可用会记录警告，已经提交的正文不会谎报为未保存。

个人会话的进化备份保存在本人根 `memory/.evolution_backups/personal-*.json`，最多保留最近 10 个，不再把个人正文复制到 Agent 工作区。撤销须满足同账号、同租户、原工作区及清空 generation，个人正文经统一发布服务恢复；清空后旧备份不能恢复正文。已有旧 Agent 备份缺少可靠个人归属，不做自动迁移。编辑进化日志本身仍只修改记录，不执行撤销。

## 历史个人数据迁移

正常已有个人文件原位显示，不需要迁移。迁移工具仅供具有该部署文件访问权限的本地运维使用，每次明确一个租户和一个有效成员；不提供 HTTP 上的任意用户选择。

默认预览只读，身份库用 SQLite `mode=ro`，不初始化数据库、不创建用户目录/锁、不恢复发布、不写正文或索引。只扫描该租户绑定 Agent 的历史 `memory/users/<uid>/*.md` 和有本人归属的索引分块；不扫描共享/会话内容。只有索引的内容会标记为不完整恢复片段。已清空的作用域及登记过删除的条目不能自动重放。

```bash
.venv/bin/python scripts/personal-memory-migrate.py --config /absolute/config.json \
  --tenant TENANT_ID --user USER_ID > /secure/path/preview.json

.venv/bin/python scripts/personal-memory-migrate.py --config /absolute/config.json \
  --tenant TENANT_ID --user USER_ID --mode apply --plan /secure/path/preview.json

.venv/bin/python scripts/personal-memory-migrate.py --config /absolute/config.json \
  --tenant TENANT_ID --user USER_ID --mode rollback --plan /secure/path/preview.json
```

先审查 preview 的候选/冲突/排除项。建议每批不超过 50 个候选：可缩小 `entries` 清单，保留同一 `batch_id` 和 owner 信息；执行仍会重新发现来源和核对指纹。报告不含正文，执行结果逐条列出成功、已导入、来源变化或冲突。中断后用相同清单续跑，不覆盖现有内容或用户的新编辑。

备份和账本位于本人根 `.memory-migration/`，仅备份本人源文本，不复制混有其他用户数据的整库。建议备份保留 30 天并纳入部署备份策略；本次没有创建自动删除任务。撤回按批次、内容版本执行，之后被用户编辑的条目保留。保存预览与执行报告供追踪；不要把含正文的备份当作公开日志上传。

本次只对部署中的截图账号做了只读预览：已有个人文件 1 个、迁移候选 0 个。未在真实账号执行迁移、删除、清空或写入。

## 验证范围

隔离数据覆盖两个账号、两个租户、两个获准 Agent 的真实 memory_add → 文件 → 正式 HTTP 页面接口 → 跨 Agent 检索 → 重新登录，以及另一账号/租户不可见。模型输出在自动摘要/梦境测试中受控，产物通过真实发布服务持久化；未调用外部模型计费服务。

Chrome 的实际加载页面已验证两标签、Markdown、Cmd+S、未保存提醒、冲突提示、重新读取版本后的覆盖保存及清空范围提示。删除/清空的实际写入在隔离 WSGI/API 测试完成。初次后续浏览器验证曾被工具错误阻断；补验已重启实际后端，并确认真实账号文件详情出现编辑、删除按钮。Desktop 内嵌控制台复用同一模板和脚本，但新版本 Desktop 完整实机写操作尚未验收；任务 7.2 保持未完成。独立旧 React MemoryPage 的显式 Agent 兼容路径不属于本次 Web 控制台切片，没有伪称其已改为个人域。
