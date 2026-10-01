本 delta 的新增布局及验收仅适用于 Web 普通单智能体。为完整替换已有 requirement 而保留的其他范围条款不构成本 change 的适配或测试任务；Desktop 客户端和 OpenCode 类智能体均不在交付范围内。

## MODIFIED Requirements

### Requirement: Preserve application behavior and brand ownership

配色切换 MUST 仅更新视觉与本地偏好，不重载页面、不重置认证或会话、不清除输入、不打断正在显示的流式回复。系统 SHALL 保留既有业务菜单和权限归属，将页面与 Agent 信息合并为单顶栏。普通单智能体空态 SHALL 使用 `agent-chat-onboarding` 的专属引导和建议问题；其他模式保持原首页。已有会话继续使用底部输入，其他业务页面沿用既有深浅适配。用户品牌来源与品牌设置中的局部深浅预览 SHALL 保持独立，Desktop 主题选择 SHALL 不受本能力修改。

#### Scenario: Switch during a conversation
- **WHEN** 用户已输入草稿或正在接收流式回复时切换配色或明暗
- **THEN** 草稿、附件、选中 Agent、业务会话与流式展示连续保留，代码高亮跟随解析后的明暗

#### Scenario: Choose multiple tenant-visible Agents
- **WHEN** 租户接口返回多个可聊天智能体，或当前会话已加入其他智能体
- **THEN** 输入区模型选择右侧的智能体入口保持可用，兼容租户接口字段与旧版启用字段，仅提供当前租户可聊天的切换或邀请候选
- **AND** 用户可在同一会话连续添加、移除成员而不丢失草稿；375px 窄屏菜单不超出视口，已有成员的移除操作不因候选列表缩减而消失

#### Scenario: Keep brand preview separate
- **WHEN** 用户切换个人配色或在品牌设置中切换局部预览明暗
- **THEN** 个人外观不写品牌名称、Logo 或描述，品牌局部预览也不覆盖个人外观选择

#### Scenario: Deliver the explicitly requested homepage layout
- **WHEN** 用户打开普通单智能体的新会话
- **THEN** 单顶栏、侧栏新建对话和原工具栏保留，专属建议问题位于输入框上方；品牌和业务菜单来源保持原归属
- **AND** 配色切换不切换页面结构，经典配色保留颜色方向而不恢复旧首页布局

### Requirement: Responsive task-oriented homepage

普通单智能体空白首页 SHALL 依次显示头像与名称、简短介绍、使用说明、最多四个建议问题和原输入框；缺少问题配置时 SHALL 显示四个默认问题。其他模式保留的通用入口 SHALL 保持原数量和行为。

系统 MUST 复用同一个输入框及其附件、事件监听、工具栏和会话状态；已有消息的会话 SHALL 使用原消息滚动与底部输入。三语界面与现有配色明暗组合 SHALL 可用，管理员正文保持原文。

普通智能体欢迎区 SHALL 减少标题与上方留白、将输入文本区初始高度设为约两三行，并取消该模式精确纵向居中的要求。建议问题在 Web 宽屏双列、窄屏单列；长文本自然换行，高度不足允许整体滚动，MUST NOT 裁剪文字或操作来强行满足首屏。原工具栏和菜单行为保持不变，布局调整不得造成遮挡。

#### Scenario: Start from a suggested task
- **WHEN** 用户在空输入、无附件且会话空闲时激活普通单智能体建议问题
- **THEN** 按 `agent-chat-onboarding` 直接发送；已有内容时入口禁用并说明原因，不覆盖草稿

#### Scenario: Transition between empty and active chat
- **WHEN** 首条消息发送、历史会话恢复，或用户新建对话
- **THEN** 空态与消息态布局相应切换，输入框节点与附件操作保持可用，新建行为复用现有会话管理而不关闭其他会话的后台流

#### Scenario: Desktop grid and narrow column fallback
- **WHEN** Web 普通单智能体首页在宽屏浏览器与 560px 以下窄屏浏览器分别显示
- **THEN** Web 宽屏问题双列、窄屏单列，问题完整可读且无横向溢出

#### Scenario: Short or narrow viewport
- **WHEN** 首页显示在 375px 窄屏、1280×720 视口或采用较大字体
- **THEN** 高度不足时整体可滚动，输入、问题及原菜单操作可达，无横向溢出；消息态保留独立消息滚动

#### Scenario: Primary actions visible with short onboarding copy
- **WHEN** 1440×900、100% 缩放下显示本 change 的简短示例文案
- **THEN** 介绍、使用说明、四个建议问题及输入发送入口在首屏可见

#### Scenario: Hero group is vertically centered
- **WHEN** 未采用普通单智能体引导的其他模式仍显示原有通用空白首页
- **THEN** 原模式继续按实际高度居中，普通单智能体使用自然内容流布局

#### Scenario: Centering keeps the task entries intact
- **WHEN** 保留通用首页的模式高度不足以同时容纳居中输入组与六个入口
- **THEN** 原模式整体可滚动，六个入口仍位于输入组下方且可达，不被裁剪、隐藏或缩小

#### Scenario: Composer menus stay reachable below the centered input
- **WHEN** 用户在空白首页展开原有指令、附件、工作空间、模型或智能体菜单
- **THEN** 保持菜单高度受可用空间约束及必要的内部滚动，末项与关闭操作可达，不因新的输入框位置发生遮挡
