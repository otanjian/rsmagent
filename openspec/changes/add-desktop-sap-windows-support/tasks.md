## 1. 宿主与文案

- [x] 1.1 放开页面读取与登录记忆的 Windows 宿主门禁，统一 `isSapDesktopHost`
- [x] 1.2 更新确认对话框、工作台开关与错误提示为桌面平台表述
- [x] 1.3 更新项目插件、skill、boundaries 与可升级摘要

## 2. 验证

- [x] 2.1 宿主单测：`win32` 可读取，`linux` 拒绝
- [x] 2.2 运行相关回归：桌面 page-read / login-memory 18 通过；工作台前端与插件 92 通过。本机无可用 Python 虚拟环境，页面读取 Python 子集未在此机执行
- [x] 2.3 更新使用说明，标明 Windows 与 macOS 均支持宿主能力
- [x] 2.4 运行 `python scripts/check_change_deltas.py add-desktop-sap-windows-support`：delta 半边无 finding（`MODIFIED` 标题与基线逐字匹配、基线 scenario 标题全部保留）；仅剩 3 条仓库级 `seam:` 覆盖噪声，在 `add-desktop-local-runtime-with-enterprise-services` 上同样复现，不属本 change 范围。本机无 OpenSpec CLI，故以该脚本加手工结构校验代替 `openspec validate --strict`
