## ADDED Requirements

### Requirement: 面向用户品牌标识统一为 RSM Code

开放代码智能体（opencode fork）面向用户的品牌标识 SHALL 统一为 `RSM Code`，至少覆盖 Web 应用标题、品牌 Logo 与图标、界面文案，以及模型系统提示词中的自我认知。MUST NOT 在面向用户的界面或提示词中继续以 `opencode`/`OpenCode` 作为主品牌名。品牌替换 MUST NOT 改变功能行为、协议字段或数据归属；上游 `LICENSE` 与版权声明 MUST 保留。

#### Scenario: Web 应用标题与资产

- **WHEN** 浏览器加载编码 Web 应用
- **THEN** 标题、favicon 与品牌 Logo 呈现 `RSM Code`，不出现上游品牌名

#### Scenario: 模型自我认知

- **WHEN** 用户向编码智能体询问「你是谁」
- **THEN** 应答以 `RSM Code` 自我标识，不出现 `opencode`/`OpenCode` 上游品牌名

### Requirement: 模型自我认知经运行时插件解耦且不改上游跟踪文件

系统 SHALL 通过运行时插件改写系统提示词中的上游身份句与外链，使品牌归属生效；MUST NOT 为达此目的编辑上游 `session/prompt/*.txt` 或其他上游跟踪文件（覆盖式同步会丢失）。插件 SHALL 位于上游覆盖区之外，且覆盖所选模型实际使用的全部提示词来源。

#### Scenario: 上游文件零改动

- **WHEN** 审查上游 `session/prompt/*.txt` 与相关文件
- **THEN** 其内容未被品牌定制编辑，身份改写由插件在运行期完成

#### Scenario: 覆盖全部模型

- **WHEN** 用户切换到不同模型再询问身份
- **THEN** 各模型应答均以 `RSM Code` 自我标识，不因提示词来源不同而回退到上游品牌

#### Scenario: 钩子失效可发现

- **WHEN** 上游移除或改名所用的提示词改写钩子
- **THEN** 身份冒烟检查失败并暴露，而非静默回退到上游品牌

### Requirement: Web 品牌经既有构建覆盖层承载且不改上游跟踪文件

Web 品牌呈现 SHALL 经既有构建覆盖层（项目现存的 vite 覆盖配置）在构建期注入，覆盖应用标题、图标与 manifest、界面文案、品牌 Logo 与对外链接；MUST NOT 为达此目的编辑上游 `index.html`、i18n 字典或组件源文件等跟踪文件。覆盖层 SHALL 以锚点守卫在定制位置缺失时明确失败。

#### Scenario: 标题与图标

- **WHEN** Web 应用加载
- **THEN** 标题为 `RSM Code`，图标与 manifest 指向品牌资产

#### Scenario: 界面文案

- **WHEN** 用户打开含品牌文案的界面（如 Provider 弹窗）
- **THEN** 显示 `RSM Code` 品牌，不出现上游品牌名

#### Scenario: 覆盖层锚点缺失

- **WHEN** 上游改动使覆盖层定位位置不存在
- **THEN** 构建以明确错误失败并指出位置，不静默产出未定制产物

### Requirement: 品牌与对外目标经单一配置来源承载

品牌名称与面向用户的外链目标地址 SHALL 由单一配置来源（共享常量或品牌配置）承载，插件与构建覆盖层引用该来源。系统 MUST NOT 在上游核心文件中就地散落硬编码的品牌名或目标地址字面量，以符合 `fork-upstream-decoupling` 对定制逻辑位于稳定接缝的要求。

#### Scenario: 更换对外地址

- **WHEN** 运营方更换对外地址
- **THEN** 只需修改单一来源，各产品面随之更新，无需逐文件编辑字面量

### Requirement: 品牌泄漏以既有检查脚本中的门禁拦截

系统 SHALL 在既有同步检查脚本中提供品牌泄漏层，检查 Web 构建产物与关键构建字面量是否残留上游品牌标识，并允许对明确白名单（包 scope、协议头、service tag、仅作来源说明的提及）豁免。该检查 SHALL 在同步后与交付检查中运行，命中非白名单条目即失败。系统 MUST NOT 以新增独立扫描工具替代复用既有检查入口。

#### Scenario: 产物残留品牌

- **WHEN** 交付检查在 Web 构建产物中发现非白名单的上游品牌标识
- **THEN** 检查失败并指出命中位置，构建不得视为通过

#### Scenario: 白名单豁免

- **WHEN** 命中项为协议头、包 scope、service tag 或仅作来源说明的提及
- **THEN** 检查按白名单豁免，不误报

### Requirement: 上游源码归属与许可证保留

去标识 SHALL 仅作用于面向用户的品牌呈现。上游 `LICENSE`、版权声明与必要的源码归属引用 MUST 保留；对仅作来源说明的 Markdown 正文与代码注释域名提及 MUST NOT 改写为运营方地址，以免文字与其所指来源不符。
