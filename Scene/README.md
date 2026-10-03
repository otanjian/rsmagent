# 场景应用

从 OneAgent 迁入 10 个分类、26 个主场景和 81 个子场景。入口仍为主服务 `/chat` 中的「场景应用」。所有场景定义及业务源文件存放在本目录。

```text
Scene/
  catalog.json                         # 分类与场景目录顺序
  skill_mapping.json                   # 场景技能名到实际技能目录的映射
  procurement_supplier/
    scene.json                         # 原始场景及子场景定义
    skills/procurement-supplier-risk/   # 原始技能、脚本和资源
    skills/tianji-business-search/      # 供应商风险的配套查询技能
  finance_voucher/
    scene.json
    frontend/workbench.js               # 原控制台中的完整功能块
    frontend/voucher-workbench-modal.html
    backend/VoucherTemplateHandler.py
    skills/finance-ledger-generator/
  production_plan/
    scene.json
    frontend/
    backend/                           # 排产 API、排产引擎和甘特图
    assets/                            # 原始示例与模板
    skills/pmc-scheduler/
  sap_data_analysis/
    scene.json
    frontend/
    backend/sap/                       # 查询计划、数据源、导出和图表
    vendor/sap-adt-cli/
    skills/sap-integration/
  ...                                  # 其余场景以原 scene.id 命名
  _shared/
    frontend/                          # 共用工作台、ERP 配置及页面适配
    backend/                           # 原始公共上传、解析与连接处理器
    host.py                            # 现有主服务登录与工作目录接口
    original.py                        # 旧调用方的模块短名称兼容映射
    http.py                            # 主服务 HTTP 接入
    frontend.py                        # 原始前端片段组合与资源服务
```

每个场景都有自己的 `scene.json`。采用同一通用工作台的场景共用 `_shared` 中的代码；纯对话场景保留原来的角色提示词。未在 OneAgent 中提供独立技能包的场景保持原样，不生成替代技能。原有 13 套业务技能和 1 套配套商查技能由主服务作为 builtin 技能发现，自定义同名技能的优先级保持不变。

## 来源与适配边界

迁入时保留独立文件及提取片段的来源和原始范围；`scene.json` 的字段值不变。后端现在使用普通 Python 导入：身份和工作目录显式引用 `_shared.host`，场景资源由 `Scene.catalog.skill_path()` 定位。`original.load()` 仅保留旧模块短名称的兼容映射，不注入全局变量、不修改 `os.path`、不安装导入钩子。

`source-manifest.json` 的 `sha256` 记录当前文件；有意适配的后端同时保存 `source_sha256` 和 `adaptation`。对照 OneAgent 时仍检查原始片段摘要，便于发现上游业务变化并合并，不能把修改后的文件冒充原样复制。

```sh
.venv/bin/python tools/verify_scene_migration.py
.venv/bin/python tools/verify_scene_migration.py --source /path/to/oneagent
```

适配层负责场景目录发现、显式资源定位、主控制台会话/发送消息接口、静态资源地址、文件上传与下载。旧的 `scenes/` 包是主服务已有接入点，配置读取已转向本目录，不再读取旧的空目录文件。

## 运行

```sh
.venv/bin/python -m pip install -r requirements-scenes.txt
.venv/bin/python -m cli.cli restart --no-logs
```

基础场景页面使用主服务自带的 web.py、原生 JavaScript 和本地 Tailwind/Font Awesome。Excel、财务报表、SAP 图表等使用 `requirements-scenes.txt` 中的原功能依赖。SAP RFC 还需要运行环境中的 SAP NW RFC SDK 与 PyRFC；ADT 方式带有原 CLI。模型、ERP、质量追溯和外部查询服务需使用本项目的实际配置。未复制 OneAgent 的账号、密钥、工作区或历史业务数据。

HTTP 入口复用主服务已有登录与工作目录解析。场景使用沿用 `chat.use`，原有配置管理操作复用主服务管理员身份。SAP 领域权限统一声明在 `auth.policy.SAP_DOMAIN_PERMISSIONS`，可以授予自定义角色；查询及自动修正后的计划都检查权限。工作台上传在实际写入时通过 `common.safe_fs` 锚定工作区，并校验最终文件名。ERP 配置面板可以从工作台中的连接管理入口打开。

## 验证

`tests/test_scene_original_runtime.py` 验证原上传、Excel 解析、multipart 导入、凭证模板、排产结果/历史、气袋模板、连接配置及静态资源入口；`tests/test_scene_skills.py` 验证目录、原始文件校验、技能发现和自定义覆盖。外部 ERP、SAP RFC、质量追溯服务及真实模型调用需要连接到相应系统后验收。
