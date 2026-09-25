## Why

工作台智能体较多时只能逐卡查找。智能体配置已经具有可维护的多值标签，直接用它搜索与筛选，可以沿用应用场景页的交互，同时避免另建岗位映射。

## What Changes

- 在工作台标题下增加关键词搜索、带动态数量的单选标签、结果数与清除条件。
- 标签来自当前用户可见智能体，支持“全部”和“未打标签”；超过一行可展开。
- 管理页标签输入支持中英文逗号，去空白、空项和重复项，仍存储为现有字符串数组。
- 补齐租户工作台投影的职位、分类、标签；保留权限、启动动作、排序及加载错误行为。
- 页面内保留筛选，账号/租户切换重置；浅色、深色、窄屏可用。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `agent-workbench`: 搜索、标签筛选、动态计数、状态隔离与标签维护规范。

## Impact

- 数据唯一归属为现有 AgentProfile.tags 与智能体配置；不依赖帮助站场景目录，无数据库迁移或新增接口。
- 修改 channel/web/chat.html、console.js、console.css、i18n/agents.js 与 fork/handlers/agents.py。
- 依赖既有 agent-workbench 最小投影与对象范围能力，以相关回归验证为接入门槛；无新增跨 change 前置。
- 工作区存在其他进行中的改动，本 change 只增量修改所需区域。
