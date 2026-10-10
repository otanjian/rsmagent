## MODIFIED Requirements

### Requirement: Responsive task-oriented homepage

空白首页 SHALL 依次显示居中的欢迎介绍、真实输入框、六个快捷入口及简短提示；每条快捷入口 SHALL 提供一个分类色调图标、标题与一句价值说明。已有消息的会话 SHALL 使用原消息滚动与底部输入。系统 MUST 复用同一输入框及其附件、事件监听和会话状态，不因布局切换创建第二套输入框。三语与三配色明暗组合 SHALL 可用。

欢迎介绍与输入框组成的一组 SHALL 作为一个整体在可视区纵向居中，桌面与窄屏 SHALL 使用同一居中口径。快捷入口与提示 SHALL 保持在该组下方，允许首页在高度不足时整体滚动，MUST NOT 为满足居中而裁剪、隐藏或缩小快捷入口或输入框。居中偏移 SHALL 依据顶栏与输入组的实际高度计算，MUST NOT 写死为与内容无关的固定距离。输入区菜单 SHALL 根据各自触发入口使用向上展开的锚点，长列表 SHALL 在菜单内部滚动；首页 MUST NOT 将指令、引用或模型菜单强制放到输入组下方，也 MUST NOT 用输入组下方的剩余空间压缩工作空间、模型、智能体、指令或引用菜单。菜单 MUST 保持末项可达，窄屏宽度 MUST 不造成横向溢出。

#### Scenario: Start from a suggested task
- **WHEN** 用户点击任意首页快捷入口
- **THEN** 本地化提示填入唯一输入框并可继续编辑，不自动发送或触发服务端任务

#### Scenario: Transition between empty and active chat
- **WHEN** 首条消息发送、历史会话恢复，或用户新建对话
- **THEN** 空态与消息态布局相应切换，输入框节点与附件操作保持可用，新建行为复用现有会话管理而不关闭其他会话的后台流

#### Scenario: Desktop grid and narrow column fallback
- **WHEN** 首页在桌面视口与 560px 以下窄屏分别显示
- **THEN** 桌面按三列两行等宽排布六个快捷入口，窄屏收敛为单列且卡片内容不截断，两种宽度均无横向溢出

#### Scenario: Short or narrow viewport
- **WHEN** 首页显示在 375px 窄屏或 1280×720 视口
- **THEN** 首页整体可滚动，输入、快捷入口与关闭操作可达，无横向溢出；输入区提示在放不下时独占一行完整可读，消息态保留独立消息滚动

#### Scenario: Hero group is vertically centered
- **WHEN** 空白首页在桌面视口或窄屏视口显示
- **THEN** 欢迎介绍与输入框组成的一组的纵向中心与可视区中心一致（允许 1px 舍入误差），顶栏、输入组或视口高度变化后按实际高度重新居中，不出现与内容无关的固定偏移

#### Scenario: Centering keeps the task entries intact
- **WHEN** 可视区高度不足以同时容纳居中后的输入组与全部快捷入口
- **THEN** 首页整体可滚动，六个快捷入口仍完整位于输入组下方并可滚动到达，不被裁剪、隐藏或缩小

#### Scenario: Composer menus stay reachable below the centered input
- **WHEN** 用户在空白首页展开此前被放在居中输入组下方或受其剩余空间压缩的菜单
- **THEN** 菜单按各自规定的上方锚点展开，不再统一放到输入组下方；超过高度上限的列表在菜单内部滚动，末项可达

## ADDED Requirements

### Requirement: Composer popovers follow their trigger anchors

工作空间与模型选择菜单 SHALL 紧邻工具栏上方展开，最大高度为 380px，不随输入框增高而移到整张输入框上方。智能体选择菜单 SHALL 在桌面位于头像按钮上方约 6px 并右对齐，最大高度为 380px；在手机窄屏 SHALL 使用输入框范围内的宽度并在工具栏上方展开，避免仅以头像右边缘对齐造成横向越界。

`/` 指令菜单和 `@` 引用菜单 SHALL 在整张输入框上方约 6px 展开，左右边缘与输入框对齐，最大高度分别为 320px 和 300px。所有上述菜单 SHALL 按内容自然伸展，超过对应上限时内部滚动，MUST NOT 为少量条目强行填满最大高度。首页和已有会话 SHALL 遵循相同的菜单锚点语义；显示菜单 MUST NOT 清空草稿或改变当前工作空间、模型、智能体和业务会话。

#### Scenario: Open workspace and model pickers next to the toolbar
- **WHEN** 用户在空白首页或已有会话中展开工作空间或模型菜单，并增高输入框
- **THEN** 菜单仍紧邻工具栏上方，采用 380px 高度上限，长列表末项可通过内部滚动到达

#### Scenario: Show several selectable agents
- **WHEN** 用户在桌面点击头像按钮，列表包含多个可选智能体
- **THEN** 菜单位于头像按钮上方并右对齐，短列表完整显示，长列表可扩展到 380px 后内部滚动，不被首页公式压缩到只剩一两项

#### Scenario: Open agent picker on a phone
- **WHEN** 用户在 375px 宽的手机布局中打开智能体选择菜单
- **THEN** 菜单宽度限制在输入框及视口内，贴近工具栏上方展开，最后一项可滚动到达

#### Scenario: Enter slash or mention near the bottom of the viewport
- **WHEN** 输入框位于页面较低位置，用户输入 `/` 或 `@` 并产生长候选列表
- **THEN** 对应菜单出现在输入框上方，分别遵循 320px 或 300px 高度上限并内部滚动，不因向下展开被屏幕底部截住

### Requirement: Workspace folder hover shows a compact path

工作空间文件夹按钮的目录悬停提示 SHALL 仅显示当前有效路径的最后两个非空路径段，兼容 POSIX 与 Windows 路径分隔符，展示时统一以 `/` 分隔并忽略末尾分隔符。只有一个非空路径段时 SHALL 显示该段；根路径 SHALL 保留根标识；路径为空时 SHALL 回退到本地化的选择工作空间提示。该规则 SHALL 同时适用于默认工作空间与显式选择的项目目录。

缩短 MUST 仅用于悬停展示，MUST NOT 修改原始路径、项目名称、工作空间选择状态、服务端请求或文件操作目标。切换智能体、会话或项目后，提示 SHALL 从最新有效工作空间状态重新计算，继续遵守既有作用域与过期响应处理。

#### Scenario: Hover over a default workspace folder
- **WHEN** 默认工作空间路径为 `/agents/assistant/user/usr_demo`，用户悬停文件夹按钮
- **THEN** 提示显示 `user/usr_demo`，原始工作空间路径及默认空间状态保持不变

#### Scenario: Hover over a Windows project path
- **WHEN** 当前项目路径为 `C:\work\projects\demo\`
- **THEN** 提示显示 `projects/demo`，项目操作继续使用原始 Windows 路径

#### Scenario: Handle short or unavailable paths
- **WHEN** 路径分别为 `/workspace`、`/` 或空值
- **THEN** 提示分别显示 `workspace`、`/` 或本地化的选择工作空间提示，不产生空白路径提示

#### Scenario: Refresh the selected project hint
- **WHEN** 用户从一个项目切换到另一个项目，或恢复默认工作空间
- **THEN** 提示根据当前有效目录更新，按钮项目名称和实际选择结果不受路径缩短影响
