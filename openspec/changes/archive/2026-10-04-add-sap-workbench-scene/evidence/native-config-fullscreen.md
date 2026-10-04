# 原生配置、OpenSpec 与 SAP智能助手交互

2026-10-04。本轮范围为 SAP 场景原生 OpenCode 接入和界面调整；实际 SAP 业务输入、页面自动操作和真实供应商对话测试仍按用户要求暂缓。

## 已完成行为

- SAP iframe 保留原生全宽显示；助手标题改为「SAP智能助手」。展开后在页面内并排显示，提供全屏、还原和收起。
- 隐藏 OpenCode 内部顶部空白工具栏和「会话／更改」紧凑页签，由场景 HTML 注入样式完成，没有修改 OpenCode 构建。
- 分隔线可左右拖动，方向键每次微调 24px，Home/End 定位边界；窄屏和全屏隐藏手柄。拖动完成/取消后恢复 iframe 指针事件，不重建两侧 iframe。
- 私有 native 监听器按原生全局/项目配置发现模型、agent、工具、MCP、技能和命令。网关保留所有者/来源校验，转发原生 API 与 WebSocket，不限制为单模型或两个只读工具。
- 四项 OpenSpec 技能与 `/opsx-*` 命令直接发现项目已有安装，不维护场景技能副本。原生 skill/bash 用隔离项目和本地供应商替身实际执行技能加载与 `openspec --version`。
- 独立会话库保持历史、远端会话 ID 和用户有效模型选择。旧强制 sap agent/provider 在原生目录不存在时迁移为原生默认。
- 模型 provider 登录是原生数据库中的 credential 记录。启动时只读原全局库继承这些记录，不复制会话、正文或全局登录写入。同步索引仅包含 integration/id/time，权限为 0600。来源轮换/删除更新已继承连接，私有实例主动修改的连接保持。没有将密钥输出到浏览器、证据或项目文件。

## 验证

- [配置目录比对](native-config-catalog.json)：相同全局/项目配置下临时 native host 和原 `4096` 的模型、agent、技能、命令、MCP 配置目录完全一致，模型均为 40 个。初次发现缺少 DeepSeek 登录导致 4 个模型不可用，补齐原生凭据继承后重新比对通过；没有通过伪造目录代替模型执行配置。
- Bun 适配器：6 pass / 129 assertions，含原 host 隔离、native 技能/CLI、多 provider 配置别名和凭据继承/轮换/删除/私有修改/独立历史。
- Python native/runtime/identity/lifecycle/manager 组合：234 pass；包含未屏蔽的 API、二进制/文本双向 WebSocket、204、默认模型、旧选择迁移及样式注入。
- 相关前端 Node：102 pass；包括 iframe 对象保留、草稿/会话不变、全屏/Escape、拖动边界、取消与键盘操作，原 coding 和其他场景入口回归。
- [服务重载](assistant-resize-header-services-reloaded.json)：Web 9899、桌面 9876 健康 200，沿完整原环境和 Electron 原恢复流程，主密钥与平台配置保留。重载前通过正常界面关闭视图，恢复同一工作台和原生对话；未新建业务单据或发送测试业务 prompt。
- Chrome 实测视口 1441×698：助手初始 461px，方向键调整到 485px，鼠标向左拖动 200px 后为 685px；全屏为 x=0/y=0/1441×698，还原为 685px。Home=320px、End=1115px，最终留在 680px。拖动结束标记 false、iframe pointer-events 恢复 auto，前后 iframe 名和工作台 ID 保持。
- 原生内部工具栏 computed display=none、height=0；全屏 SAP 区与分隔线隐藏。截图只保留顶部界面，避免复制用户现有对话内容。

![全屏工具栏隐藏](assistant-header-hidden.png)

![页内助手及分隔线](assistant-docked-resizable.png)

## 边界

本轮没有修改 `rsmcode/opencode`、普通智能体执行器、平台数据库或其他场景代码。主要改动在场景 native runtime、适配器入口/凭据继承和前端，另更新 SAP 资源摘要与专项测试、change 文档。其他既有工作区改动保持。

原生工具按 OpenCode 权限与配置执行；场景桥的审批、提交开关和模型配额不约束原生 MCP/bash。跨域 SAP iframe 自动控制、真实提交、SSO/统一登录和生产权限/计量仍属旧计划未完成项，不以本轮目录/布局和隔离供应商测试代替验收，不归档 change。
