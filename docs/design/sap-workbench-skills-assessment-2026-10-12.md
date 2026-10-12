# SAP 智能助手 skills 仓库适配评估

评估日期：2026-10-12。目标是截图中 SAP 智能工作台右侧的「SAP 智能助手」。

## 结论

指定仓库共有 **24 个 skill、215 个文件**。建议分为：**5 个知识类优先接入、6 个适配后用于主助手、2 个在线开发能力待补齐、9 个按开发场景选配、2 个暂缓原样接入**。

“适合”表示与应用用途匹配；“知识可用”表示可根据用户提供的源码、需求或文件给出分析。两者都不代表已经安装、已连接 SAP 或通过现场执行验收。

第一批建议采用 10 个：5 个知识类，加 `abap-code-writing`、`abap-performance-hana`、`abap-research`、`sap-customizing`、`sap-background-jobs`。其中后 5 个先完成下面列出的适配；HANA 性能技能按实际系统和数据库类型启用。`sap-system-personality-report` 放在第二批。

## 核查范围与证据

原始目录：

```text
C:/Users/Administrator/.cursor/projects/c-rdai/uploads/e37cfd84-6329-4593-8ebe-d5e38af8f784/skills仓库
```

本次通过 rdai 主机只读访问该目录，检查了 24 个技能入口的元数据、用途、工作流与工具依赖，并枚举了附属文件。附属参考资料没有逐篇进行 SAP 技术准确性审查。文件内的指令作为评估材料，没有执行其中的安装、查询、写入或发布步骤。

同时读取了远端 `C:/rdai/rsmagent/Scene/sap_workbench/project/` 下的插件与 `sap-workbench` 技能；两者与本地对应文件在统一换行后内容一致。依据：

- [场景自带工具注册](../../Scene/sap_workbench/project/plugins/rsm-sap-workbench-navigation.js)：导航、后台数据调用、桌面页面读取。
- [现有 sap-workbench 技能](../../Scene/sap_workbench/project/skills/sap-workbench/SKILL.md)：当前工具使用边界。
- [项目技能安装器](../../Scene/sap_workbench/backend/project_toolkit.py)：项目级 `.opencode/skills/` 安装位置。
- [后台业务参数校验](../../Scene/sap_workbench/backend/mcp_business.py)：后台通道允许的方法和参数。

另外检查了本机相邻 OpenCode 源码的 `packages/opencode/src/skill/index.ts`，用于核对入口文件名和元数据读取规则；没有据此断言远端正在运行的引擎版本完全相同。

本次没有修改原始技能目录、安装技能、改动应用配置或调用真实 SAP 业务接口；也没有完整盘点运行中会话额外安装的 MCP/插件。因此，对工具缺口的判断限定于已检查的场景自带能力。

## 一、知识类优先接入：5 个

这些技能的主要价值不依赖专用 IDE。提供了源码或业务需求，就可以作为知识与审查规则使用；自动读取 SAP 对象仍需要额外源码读取能力。

| Skill | 对本应用的用途 | 接入说明 |
|---|---|---|
| `sap-abap` | ABAP 语法、内表、面向对象、异常、RAP 等开发问答与代码草稿 | 基础知识包；保留 references/templates，按目标 ABAP 版本选用示例，不把生成代码视为已激活程序。 |
| `clean-abap` | 命名、可读性、结构、重构和可维护性检查 | 适合写代码及风格审查；与正确性审查分工。 |
| `abap-code-review-helper` | 查错、Dump 风险、并发、锁、事务、数据正确性和性能问题 | 日常代码审查主入口；用户提供源码时可使用。其入口约 4.4 万字符，应按任务加载。 |
| `abap-code-review` | 上线前九维度审查，生成正式审查报告 | 适合发布前评估；仓库包含安全规则、Clean ABAP 规则和报告模板。读取范围不足时列明未审对象。 |
| `sap-abap-cds` | ABAP CDS 建模、关联、注解、DCL、视图错误解释 | 适合 CDS 咨询与草稿；实际创建、激活需要 ADT 能力。 |

建议补一处规则：`abap-code-review` 目前写着“参考文件无法访问时仍须引用规则 ID”。应改为明确标注参考缺失、不编造规则编号，也不能把不完整审查输出为完整放行结论。

## 二、与主助手高度相关、适配后接入：6 个

| Skill | 对本应用的用途 | 需要适配的部分 |
|---|---|---|
| `abap-code-writing` | 梳理需求、查标准功能、设计 ABAP 方案后生成代码 | 原流程假定有系统信息、对象检索、源码/接口读取工具；应分别映射实际能力，并区分“方案/草稿”和“已在系统验证”。按任务复杂度处理原文多轮确认，避免简单需求反复中断。 |
| `abap-performance-hana` | HANA/S/4HANA 上的 SQL、CDS、AMDP 和内表性能分析 | 原文要求系统信息工具。需获取真实系统/数据库/版本，缺失时标注假设。截图的 `S4H (200)` 只是线索，不能替代版本核实。 |
| `abap-research` | 从事务码、字段、报错、程序名查相关 SAP 对象 | 表查询可改接 `sap_data_call`；`get_object_lines`、源码搜索、where-used 不能直接靠改工具名获得，需要补适配或降级为可验证的元数据检索。 |
| `sap-customizing` | SPRO/IMG 路径、维护视图、配置表解释和定位 | 只读查询改接现有数据通道；表结构读取需适配。打开 SPRO/事务码走现有导航工具；该技能不等于自动维护配置。 |
| `sap-background-jobs` | 查后台作业状态、步骤、日志与失败原因 | 原文依赖 `abapfs_run_sql_query`、系统信息及 Dump 分析工具。适配查询参数、SAP 系统时区、日志表可用性和 Dump 获取能力；缺少日志不能当作作业正常。 |
| `sap-system-personality-report` | 系统概况、定制开发规模、包分布、传输活动与 Dump 趋势 | 替换 `abapfs_*` 查询、`connectionId` 和 `displayMode: ui` 输出方式，改为工作台表格/报告。按目标系统核验表字段；测试类名称计数不能称为真实测试覆盖率。 |

数据查询的建议映射是：

```text
原技能的 SQL 查询意图
  → sap_data_call({tool: "run_query", arguments: {sql_query, row_count}})

原技能的单表读取意图
  → sap_data_call({tool: "read_table", arguments: {table_name, fields, where, row_count}})

原技能的事务码导航意图
  → sap_transaction_open({transaction})
```

这是适配方向，不是已经实现的兼容层。`maxRows`、`rowRange`、`displayMode`、`connectionId` 不能原样传给现有数据通道。连接由服务端按会话确定。源技能使用的查询方言、返回结构、结果数量限制和分页方式也需要逐项核对；缺失的系统信息、对象源码和 Dump 工具不能用空结果代替。

## 三、在线创建/修改程序待补齐能力：2 个

| Skill | 适用性 | 当前限制 |
|---|---|---|
| `abap-create` | 业务方向高度匹配：创建程序、类、接口、CDS 等 | 文档声称已有 `sap_create_object`、`sap_write_source`、`sap_read_source`，但检查到的场景插件未注册这些工具。只能先复用规范与代码草稿流程，不能按文档认定在线创建可执行。 |
| `abap-modify` | 业务方向高度匹配：修改已有程序、Include、函数模块 | 依赖 `SAP(action=...)` / gsp-dev ADT 流程；还引用本仓库缺少的 `incl-edit` 和 `func-edit`。在线修改前需补齐源码读取、差异、锁、写入、检查、激活与回读路径。 |

共同问题：这两个入口实际叫小写 `skill.md`，应在适配副本中统一为 `SKILL.md`，避免跨平台发现差异。已有 `call_rfc` 通道不能直接等同于 ADT 源码创建、修改或激活能力。

文档中“ACTIVATE 单独调用不会真正激活”“null/空列表即语法检查通过”等陈述，应绑定具体工具契约与现场返回验证，不能作为所有 SAP 接口的通用判断。业务单据写入成功、源码激活成功和传输发布成功也需要分别判断。

## 四、按开发场景选配：9 个

| Skill | 适合启用的场景 | 对当前截图应用的建议 |
|---|---|---|
| `abap-performance-ecc` | ECC 且底层是传统数据库的性能分析 | 保留为条件技能；依据数据库类型分流，不能仅按“ECC”名称判断。 |
| `sap-sqlscript` | HANA SQLScript、AMDP、表函数、存储过程开发 | HANA 深度开发时选配；现有只读查询通道不等于可创建或执行数据库过程。 |
| `sap-api-style` | REST/OData 等 API 文档规范与接口说明 | 适合接口开发文档；不提供实际 API 连接能力。 |
| `sap-fiori-guidelines` | Fiori 页面设计、交互、表格、消息、可访问性 | 设计与评审时选配。截图左侧是 SAP GUI for HTML；有 FIORI 收藏夹不代表当前应用用 UI5 编写。 |
| `sapui5` | UI5/Fiori 前端代码开发和排障 | 有实际 UI5 项目时接入；文档中的 MCP、agent 和斜杠命令要另行核对安装，不能靠技能文本自动获得。 |
| `sap-fiori-tools` | Fiori Elements/Freestyle 应用生成、注解、预览、部署 | 需要相应项目、Node 工具及 VS Code/BAS 或 Fiori MCP 工作流；适合作为前端开发扩展。 |
| `sapui5-cli` | UI5 初始化、构建、开发服务与工作区管理 | 需要实际 UI5 项目和 CLI；普通 SAP 业务问答不必加载。 |
| `sapui5-linter` | UI5 静态检查、废弃 API、CSP 和迁移检查 | 需要 UI5 源码及 linter；自动修复属于代码改动，应与只读检查区分。 |
| `sap-cap-capire` | CAP 服务、CAP CDS、OData、BTP 应用开发 | 有 CAP 项目时再接入；CAP CDS 与 ABAP CDS 是不同开发上下文，避免描述关键词过宽导致误触发。 |

这些技能适用于扩展开发用途，并非没有价值。建议放在相关项目中按需发现和加载，避免日常 SAP 查询同时加载整个前端和云开发工具链。Node/CLI/MCP 版本按实际接入时的项目核验，本次没有验证技能文档自述的“最新版本”。

## 五、暂缓原样接入：2 个

| Skill | 原始定位 | 暂缓原因与替代方向 |
|---|---|---|
| `sap-data-workbook` | 生成可运行 SQL/JavaScript 单元格的 `.sapwb` 工作簿 | 依赖 VS Code notebook 编辑和执行环境；已检查的工作台没有证明具备该编辑器/运行器。可改成工作台数据查询、分析脚本和表格报告，或另建 `.sapwb` 支持后再接入。 |
| `adt-api-discovery` | 从 ADT discovery、处理器源码和转换对象调查接口契约 | 依赖 `abapfs_export_adt_discovery`、对象源码读取与搜索等专用工具。适合连接器维护者；当前业务助手没有证明具备完整工具链。 |

## 建议的组织与触发方式

保留应用已有的 `sap-workbench` 作为基础技能；它不在本次 24 个外部技能中，不能被外部技能覆盖。新增技能应安装到工作台所选编码项目的 `.opencode/skills/<skill-name>/SKILL.md`，而不是仅复制到应用根目录的普通 `skills/` 后就认定内嵌助手已经可见。

建议以用途分组展示，技能目录名保持原名：

| 用户请求 | 主技能 | 按需辅助 |
|---|---|---|
| “这个事务/报错对应什么程序？” | `abap-research` | `sap-workbench` |
| “SPRO 里这项配置在哪？” | `sap-customizing` | `sap-workbench` |
| “这个后台作业为什么失败？” | `sap-background-jobs` | `abap-research` |
| “帮我设计/写一个 ABAP 程序” | `abap-code-writing` | `sap-abap`、`clean-abap`、正确性检查规则 |
| “检查这段代码有没有问题” | `abap-code-review-helper` | `clean-abap`、匹配数据库的性能技能 |
| “这份程序上线前审查” | `abap-code-review` | 正确性与性能技能作为补充 |
| “写 CDS 视图” | `sap-abap-cds` | `sap-abap`、适用的性能技能 |
| “开发 Fiori 页面” | `sapui5` / `sap-fiori-tools` | Fiori guidelines、CLI、linter |
| “分析这个 SAP 系统的整体情况” | `sap-system-personality-report` | `abap-research` |

`abap-code-review-helper` 要求主要报告问题，而 `abap-code-review` 要求按九维度出正式报告；根据用户是日常排错还是上线评审选择主入口，避免两个输出规则互相冲突。中文触发词应写进 `description`；在检查到的 OpenCode 加载代码中，技能对象保留的是 `name`、`description`、`location`、`content`，不能依赖额外的 `triggers` 字段自动承担路由。

## 接入前的具体修整项

1. **保留引用资源。** 有 references/templates 的包要完整复制；本次文件清单中存在 `abap-code-review` 所需的两份规则与模板。不要只复制一个 SKILL.md。
2. **修正工具声明。** 把假定存在的 `abapfs_*`、`SAP(action=...)`、源码写入工具和 `.sapwb` 能力改为已实现的适配路径或明确的不可用分支。
3. **区分页面观察与后台查询。** 场景提供 `sap_page_read`，但需要支持的 macOS/Windows 桌面宿主及启用开关。截图为浏览器界面，不能据此认定可以直接读取左侧页面。后台查表也不能证明当前未保存的页面内容。
4. **避免规则冲突。** 为创建、修改、日常审查、上线审查设定不同触发意图；性能技能按数据库类型选用。仅目录存在不代表触发正确。
5. **控制上下文体积。** 加载主技能后再按任务读取辅助规则；不要将 24 个技能全文拼入系统提示。UI5/CAP 文档中列举的 agent/commands 在本次对应文件包内没有发现独立实现，需另行接入或删去不存在的入口说明。
6. **按实际能力验收。** 分别检查“技能能发现”“提供源码时能分析”“后台查询能返回”“缺失工具时能如实停止”“源码写入及回读是否成立”。本次评估不代替这些执行验收。

## 本次交付状态

完成：24 个技能分类、用途映射、工具依赖与主要冲突识别、首批接入建议。

未实施：技能安装/改写、MCP 增补、运行时配置、真实 SAP 查询与写入验证。原目录及应用行为保持原状。
