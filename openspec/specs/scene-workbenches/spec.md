# scene-workbenches Specification

## Purpose
TBD - created by archiving change port-scene-applications. Update Purpose after archive.
## Requirements
### Requirement: 工作台场景分发

系统 SHALL 对 `has_workbench` 为真且含 `sub_scenes` 的场景提供工作台视图。场景中心卡片激活时 SHALL 按场景类型分发到对应工作台渲染器；无专用渲染器时使用通用工作台。工作台 SHALL 展示 `workbench_title` 与子场景面板，子场景选择后 SHALL 展示该子场景的功能模块。无专用渲染器且无子场景的场景 SKIP 工作台、按普通场景激活。

`sap_workbench` SHALL 作为明确的专用分发例外：当其声明工作台时，即使没有 `sub_scenes`，也 SHALL 打开 SAP 专属工作台；能力关闭或依赖不可用时 SHALL 展示该场景不可用状态，MUST NOT 回落普通对话。此例外不改变其他场景既有分发行为。

#### Scenario: 通用工作台渲染
- **WHEN** 场景 `has_workbench` 为真、含子场景且无专用渲染器
- **THEN** 渲染通用工作台，含工作台标题与子场景面板

#### Scenario: 专用工作台分发
- **WHEN** 场景匹配某一专用工作台类型（如凭证、财税、会计准则、财务报表审查、SAP 数据分析、质量追溯、生产排产）
- **THEN** 调用对应专用工作台渲染器，而非通用工作台

#### Scenario: 无工作台场景
- **WHEN** 不属于 SAP 工作台例外的场景 `has_workbench` 为假或按既有规则无子场景
- **THEN** 按普通场景激活进入对话，不渲染工作台

#### Scenario: SAP 工作台无子场景
- **WHEN** 用户选择声明工作台的 `sap_workbench` 且其没有子场景
- **THEN** 普通入口在配置及能力就绪后直接新建 SAP 主界面及右下角页内对话工作台，不调用普通场景激活；卡片「配置」按钮只打开配置页

#### Scenario: SAP 工作台不可用
- **WHEN** SAP 工作台功能关闭、依赖未就绪或渲染加载失败
- **THEN** 显示明确不可用或可重试错误，保留原会话，不回退到通用工作台或普通执行器

### Requirement: 子场景面板与功能模块

系统 SHALL 在通用工作台内按子场景渲染功能模块卡片，每个模块 SHALL 展示名称、图标与描述。选中功能模块 SHALL 进入该模块的交互面板（表单/查询/导入）。子场景选择 SHALL 与场景中心缓存复用同一场景数据，不重复请求。

#### Scenario: 选中子场景模块
- **WHEN** 用户在通用工作台点击某子场景的功能模块
- **THEN** 切换显示对应交互面板并加载该子场景的上下文

#### Scenario: 当前场景无子场景功能模块
- **WHEN** 场景含子场景但当前子场景无功能模块
- **THEN** 显示空态或不可用提示，不报错

### Requirement: 工作台文件导入与 ERP 元数据

系统 SHALL 支持工作台按子场景 `import_config` 上传数据文件（Excel/CSV 等）；支持文件内容以 base64 或文本提交，并按子场景 `erp_config` 展示系统选择/数据查询元数据。上传结果 SHALL 返回可读状态；文件 SHALL 保存到工作区临时目录供 Agent 读取。上传失败 SHALL 返回错误，不破坏当前面板。ERP 元数据仅作展示与查询入口，v1 不复制 OneAgent 的实际连接与凭据。

#### Scenario: 上传导入文件
- **WHEN** 用户在工作台选择文件并提交导入
- **THEN** 保存到工作区临时目录并返回成功状态，Agent 可读取该文件

#### Scenario: 上传格式非法或为空
- **WHEN** 上传文件为空或内容无法解析
- **THEN** 返回错误提示，不写入文件

#### Scenario: ERP 系统元数据展示
- **WHEN** 子场景配置了 `erp_config` 且含 `systems`
- **THEN** 在工作台展示可选系统与查询入口元数据，不携带真实连接凭据

### Requirement: 场景数据表命名规范

系统 SHALL 为场景应用引入的数据库表使用统一命名格式 `cj-{场景英文名}-{具体表名}`。`cj` SHALL 为固定「场景」前缀，场景英文名 SHALL 取自场景 `id`（如 `procurement`、`finance`），具体表名 SHALL 使用下划线小写。跨场景共享表 SHALL 使用 `cj-common-{具体表名}`。共享底座（配置/服务/接口）MUST NOT 引入数据表；仅当某场景或工作台确需本地持久化时才按此规范建表。命名冲突 SHALL 在实现时按场景 `id` 唯一性避免。

#### Scenario: 场景建表命名
- **WHEN** 某场景或工作台需要本地持久化数据表
- **THEN** 表名采用 `cj-{场景英文名}-{具体表名}`，如 `cj-procurement-supplier`

#### Scenario: 跨场景共享表命名
- **WHEN** 数据表被多个场景共享
- **THEN** 表名采用 `cj-common-{具体表名}`，不归属任一具体场景英文名

#### Scenario: 共享底座不建表
- **WHEN** 仅实现场景配置、服务与接口底座
- **THEN** 不创建任何数据表，命名规范仅作为后续持久化的固定约定

