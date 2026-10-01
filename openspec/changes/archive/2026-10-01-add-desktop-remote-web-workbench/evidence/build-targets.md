# 构建目标与运行时（task 1.5）

## 已登记目标

来源：`desktop/package.json`（electron-builder 配置）与
`.github/workflows/release.yml`、`.github/workflows/release-win7.yml`。

| 平台 | 目标 | 架构 | 说明 |
| --- | --- | --- | --- |
| macOS | `dmg` + `zip` | `x64`、`arm64` | task 1.5 起显式登记两种架构（此前依赖宿主架构默认值） |
| Windows 10/11 | `nsis` | `x64` | 标准发行线 |
| Windows 7（旧线） | `nsis`（`artifactName` 带 `win7` 段） | `x64` | 独立 workflow，安装 `electron@22.3.27` |

## Electron 版本与 WebContentsView

- 标准线：`desktop/package.json` 依赖 `electron ^33.2.0`（本机安装 `33.4.11`）。
- 旧线：`.github/workflows/release-win7.yml` 使用 `npm install --no-save electron@22.3.27`。
- `WebContentsView` 自 **Electron 30** 起提供。因此：
  - 标准线（33.x）可承载远程容器；
  - 旧线（22.3.27）**不能**，且必须在代码层面拒绝，而不是依赖用户不选远程模式。

## 旧线保护（已实现）

`desktop/src/main/remote/container-support.ts`：

- `MIN_ELECTRON_MAJOR_FOR_CONTAINER = 30`；
- `supportsWebContentsView(version)` / `canHonourRemoteMode(version)` 为纯函数，
  可被直接测试；
- `config-ipc.ts::startupMode()` 在 `mode === 'remote'` 但运行时不支持时
  **回退 local** 并记录原因，不构建远程模块、不启动 Python 后端；
- `config-ipc.ts::setMode('remote')` 在同样的运行时上**拒绝持久化**该模式，
  返回 `applied:false` 与原因，设置壳据此提示，不会留下一个下次无法启动的模式；
- `modeProjection()` 暴露 `containerSupported` / `containerUnsupportedReason`，
  设置壳据此禁用“进入远程模式”。

验证：`tests/test_desktop_remote_config.cjs`（`WebContentsView support starts at
Electron 30`、`the legacy line is forced back to local mode, with a reason`）。

## 未决

- 两种架构的 macOS 产物需要各自在对应架构上签名/公证；本轮未产出真实安装包，
  因此 task 6.2 的真实打包客户端验收仍未完成（见 `evidence/phase-1.md` 待建）。
- 旧线是否需要在 CI 中额外断言“不含远程模块”尚未加入；当前由运行时守卫覆盖，
  若后续把守卫改成编译期裁剪，需要补一条打包产物体检。
