## MODIFIED Requirements

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
