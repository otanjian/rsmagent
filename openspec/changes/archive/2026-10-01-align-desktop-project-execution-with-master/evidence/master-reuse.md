# master 复用基线证据

上游对照：`origin/master@8f1b19f1e72db0b46772f78f9c760b04b1836428`（2026-09-18，
Merge PR #3161）。本地对象已存在，`git cat-file -t` 返回 `commit`，可直接 diff。

实施基线：`rdai` 分支，起点 `7d4cf3db`（add-desktop-remote-web-workbench 已合并）。

本文记录**已经用 diff 核对过**的接缝，及每处的处理方式。未核对项明确标为未完成，
不因为文档存在就当作已复用。

## 1. 上游接缝与当前处理

| 接缝 | master 位置 | 当前状态 | 处理 |
|---|---|---|---|
| `read/write/edit/bash/ls/search_files` 工具类 | `agent/tools/{read,write,edit,bash,ls,search_files}` | 与 master 同源：`write.py`/`edit.py` 各仅 +4 行（新增 `abs_path` 返回字段），`bash`/`ls`/`read`/`search_files` 无改动 | **直接复用**：本机执行使用同一批 Python 类与 schema |
| `agent/tools/__init__.py` 注册工厂 | 同路径 | +31 行（新增 `ClientFiles`、`MemoryAdd`、`Todo`、`ExternalConnection` 注册） | **最小适配**：新增 headless 入口需从同一工厂取已登记工具 |
| `agent/workspace/project_store.py` | 同路径 | +63/-6（个人根校验、project_browser 相关） | **直接复用 + 最小适配**：新增带来源的本机绑定分支，不放宽普通 `set_project_dir()` 个人根校验 |
| `Agent.apply_project_dir()` / `effective_cwd()` | `agent/protocol/*`、`agent/workspace/*` | 当前保留（与 master 同语义） | **直接复用**：本机执行上下文提供获权实际根；禁止全局 `os.chdir()` |
| `agent/protocol/agent_stream.py` 工具门禁 | 同路径 | +396/-39（工具筛选、权限、取消、工件事件已集中） | **直接复用**：执行 v2 代理必须在既有门禁**之后**接入 |
| SkillManager / loader | `agent/skills/{manager,loader,service,types}.py` | +178/+23/+116/+8 | **直接复用 + 适配**：选择逻辑不变，新增版本包资源来源 |
| 项目工作区提示段 | `agent/prompt/{builder,workspace}.py` | +186/+308，另有新增 `shared_assets.py` | **复用文案结构**：远程上下文使用逻辑位置与真实平台 |
| `desktop/src/main/index.ts` 的 `select-directory` | 同路径 | +134/-18 | **抽取复用**：可信原生选择服务，返回范围化选择记录 |
| 工具结果 / 工件事件 / 文件卡片 | `agent/protocol/artifact.py` 等 | 保留结果外形 | **复用 + 增加来源分支** |
| 企业化新增（不回退） | master 无 | `agent/permission/isolation.py`、`agent/workspace/project_browser.py`、`agent/tools/client_files/*`、`external/*`、`todo/*`、`requirements_delivery.py`、调度授权等 | **保留**：不整体覆盖当前企业化代码 |
| Web 目录 | master 无 `channel/web/static/js/console.js`、`channel/web/fork/**` | 企业化重构产物 | **保留**：修复/扩展当前加载文件，不修改未加载的拆分脚本 |

## 2. 关键结论

- 文件工具本身**不需要重写**：本机执行复用的就是 `agent/tools` 下与 master 同源的 Python 类。
- 需要新增的只有传输、作用域、缓存、平台启动与本机产出投影（`design.md` D3/D5/D6/D8）。
- 隔离放在执行启动/资源解析边界，不能靠 cwd、字符串检查或现有 `execution_isolation` 配置值。

## 3. 未完成项（不得当作已复用）

- [ ] 逐文件 diff 复核尚未覆盖 `bash`/`ls`/`read`/`search_files` 的完整内容哈希（当前仅按 diffstat 判定无改动）。
- [ ] 未与 master 的 SkillManager 行为做真实技能回归对比（属 P3/A06/A07）。
- [ ] 未记录桌面安装包内 Python/库锁定清单（属 P4/A33）。
