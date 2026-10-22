# 规划依据（2026-10-15）

本文件原始核对时范围为文件阶段；用户随后明确同一change分两阶段交付，新增脚本/技能第二阶段。下面“脚本属于后续建设”表示第一阶段不实施，不再表示另立change；当前范围以proposal/design/tasks和两组验收矩阵为准。

后续简化评审已撤销原先新增project_files/project_scripts协议的设计，改为复用同一执行合同骨架，已定案采用v3且旧v2冻结；默认复用已有开关，独立控制须说明理由并仅收紧能力交集。以下相关原始方案记录仅供追溯，不能作为当前实施要求。

本文件仅记录只读核对与设计依据，未运行产品文件操作、构建helper或修改生产配置。

- 当前OpenSpec根为D:/AIPJ/rsmagent，schema=spec-driven；原始核对时记录.agents/.qoder/.trae技能目录为空（历史状态；本轮已读取项目OpenSpec技能，不代表当前仍为空），OpenSpec CLI可用，按CLI各artifact说明及openspec/config.yaml生成。
- `contracts/desktop/v2.json`明示win32不支持全部v2工具；原方案拟新增project_files可选协议，现按design D4改为统一v3合同及兼容验证，旧v2声明不放宽，不通过配置绕过。
- 当前fs-guard为POSIX-only且固定只读；Windows需新增平台句柄实现，写入必须另有限入口，不能直接给旧协议加write。
- `project-browser/browser.ts`读/写分别走现有只读与v2路径；只改面板不能保证模型派发正确，统一执行派发须同时接通模型与面板。
- `local-execution/sandbox.ts`脚本支持主要为macOS seatbelt，Linux落入不支持分支；不能以POSIX契约声明保证Linux脚本零开发可用。
- 记忆403资料与生产复验说明文件锁回退缺陷及其修复，不证明脚本逃逸，亦不证明本次文件边界已实现。
- harden-windows-desktop-reliability已有37/55任务完成（读取时状态），其POSIX执行/脚本门槛与本次Windows平台验收分开，不修改其任务或历史证据。
- 微软NtCreateFile、AppContainer/LPAC与Job Object文档已核对，引用见design；本次只采用有限文件helper和句柄访问，OS脚本隔离属于后续建设。
