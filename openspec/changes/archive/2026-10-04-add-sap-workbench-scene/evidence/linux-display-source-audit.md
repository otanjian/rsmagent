# 独立显示组件的源码、入口与真实桥接核验

2026-10-04 本轮仅核对部署单元与私有 loopback fixture。未启动 Colima，未重复 Docker Hub/ECR 请求，未改全局代理/DNS/证书，没有 UI/CDP、SAP、OpenCode、MCP 或模型操作。开始与结束均为 Colima Stopped、Docker context default。

## 4.1 原合同与运行验收的区别

原任务为“在场景独立部署目录提供有头 Chromium、独立显示单元、VNC/WebSocket 桥及 noVNC 资源，固定兼容版本；主服务默认启动不依赖浏览器组件在线”。其组件提供、固定来源与独立启动合同可以按下述实现和证据评估；完整 Linux 容器、实际 Chrome/Xvfb 沙箱、可视键鼠及平台场景绑定属于尚未证明的运行验收。不能反向把一次 HTML/RFB 协议成功解释成这些验收通过。本代理不修改任务勾选。

| 提供合同 | 实现/证据 | 实际限制 |
| --- | --- | --- |
| 场景独立部署入口 | `browser.Dockerfile`、`browser-compose.yaml`、`entrypoint.py` 与 `healthcheck.py` 均在场景 deployment 目录；没有接入公共 Web/桌面默认启动链 | 主服务现有按需 CDP 路径独立于本单元；未实现 noVNC 的场景节点适配 |
| 有头 Chrome 与显示 | 固定官方 Chrome for Testing 两架构归档，初始仅 about:blank；Xvfb :99、MIT-MAGIC-COOKIE-1 Xauthority、无 X TCP 暴露；Chrome 参数无 headless/no-sandbox/证书绕过 | Docker/APT/Chrome/Xvfb 实际启动仍未验收 |
| 来源及架构 | amd64/arm64 基础 manifest digest、Debian snapshot、Xvfb/x11vnc 精确包版、Chrome generation/大小/官方 MD5、noVNC/websockify SHA-256、seccomp SHA-256 均保留 | legacy builder 会先解析另一基础 stage；部署文档要求显式正确 TARGETARCH；不能把另一 stage 的 HEAD 失败认作目标架构运行失败 |
| 非 root 与只读布局 | Docker UID/GID 10001、只读根、cap_drop ALL、no-new-privileges、固定 seccomp、独立 /tmp tmpfs；profile/config/cache/Xauthority/PID 状态均置于 /tmp/sap-browser，HOME 本身不写 | 内核 user namespace 与沙箱能力尚未容器实测；不删除这些限制来换取启动 |
| 独立 VNC secret | secret 仅用于 VNC，启动前八字节格式/实际可读性检查；既有受限宿主目录与容器非 root 文件读权限说明保留 | 不是 SAP/MCP 密码或平台凭据；真实 VNC 认证未测 |
| 组件顺序与关闭 | Xvfb ready → Chrome 版本 ready → RFB ready → noVNC/WebSocket ready；监测自身进程；按原 process group 清理、处理已退出父进程后代；本轮停止竞态已修复 | 真实临时 Python 进程组证据不代替 Chrome renderer 与 Linux 容器回收 |
| 真实上游桥与资源 | 官方固定归档实际校验、解包与 shell run --help；真实 websockify 提供 noVNC HTML/JS；实际 socket/WebSocket 双向 fixture bytes 通过 | 后端为受控 RFB banner fixture，没有真实 VNC 认证/像素解码/键鼠/浏览器或场景绑定 |

未发现需要调整 Dockerfile/Compose 来源、版本、权限、路径或 CLI 的确定代码缺陷；保持既有参数。未因任务难度新增生产权限、网关或部署依赖，也没有将镜像网络失败修饰为容器通过。

## 必要窄修复：停止期间不推进启动

原 `Supervisor.wait_ready()` 只在 while 顶部检查 stopped；若 SIGTERM/停止在 ready 回调运行时发生，而回调仍返回 true，函数会返回成功。`start_unit()` 随后调用下一组件的 `start()`，原 `start()` 不检查 stopped，可在停止请求后补启一个进程。

修复仅为 `entrypoint.py` 的两个边界检查：`start()` 在 Popen 前拒绝已 stopped；`wait_ready()` 在 ready 成功后再检查 stopped，不能返回成功推进。已经 Popen 的进程仍立即登记为本监督器所有，由原 finally stop 回收，不扫描或操作其他进程。

新增真实临时 Python 单元测试在第一个组件的 readiness 期间设置停止，确认下一组件未启动，既有首个组件被关闭；另验证开始前已经 stopped 不调用任何 spawn。配合此前真实进程组/不可读 secret 和原隔离专项，普通命令为 **47 passed / 1 skipped in 0.28s**：

```sh
.venv/bin/python -B -m pytest -q \
  tests/test_sap_workbench_display_process_cleanup.py \
  tests/test_sap_workbench_display_deployment.py \
  tests/test_sap_workbench_display_upstream.py
```

## 显式真实上游组件启动

运行命令：

```sh
SAP_DISPLAY_UPSTREAM_SMOKE=1 .venv/bin/python -B -m pytest -q \
  tests/test_sap_workbench_display_process_cleanup.py \
  tests/test_sap_workbench_display_deployment.py \
  tests/test_sap_workbench_display_upstream.py
```

实际宿主为 macOS / Python 3.14.3。noVNC v1.7.0 官方归档 **726,728 字节**、websockify v0.13.0 官方归档 **57,826 字节**，均通过原固定 SHA-256；没有读取 Docker/ECR、Chrome 归档或 SAP 资源。新显式测试本身首次运行 **1 passed in 3.10s**，完整部署组合为 **48 passed in 3.46s**。

真实执行上游 `run`（shell → `python3 -m websockify`），确认 `--file-only` 支持；在本次私有随机 loopback 端口启动，实际 GET `vnc.html`、`app/ui.js`、`core/rfb.js` 均成功。通过原 `rfb_ready`、`novnc_ready`、`websocket_ready` 函数；健康检查只读 banner，停止于认证前。额外 WebSocket 客户端仅发送 fixture RFB version bytes，后端收到相同字节并经真实 upstream bridge 回显，证明双向 transport，未发送真实键鼠或浏览器页面输入。

本次下载/解包使用 TemporaryDirectory，finally 回收自身 process group 与受控 socket/thread，检查桥端口关闭，临时目录退出删除。测试默认跳过，只有明确 `SAP_DISPLAY_UPSTREAM_SMOKE=1` 才下载官方固定小型资源并启动 fixture，POSIX 以外不运行；普通应用导入/默认启动不会执行。

`browser-versions.json` 的 `runtime_verified=false` 与 `workbench_binding_verified=false` 保留。Linux 完整镜像运行仍受此前记录的上游 EOF/502 阻断，真实 Chrome/Xvfb/沙箱、认证/像素/输入及工作台接入仍没有新增运行证据。源码差异仅上述 entrypoint 停止检查、SAP 部署专项和部署/本 evidence 文档；core、普通智能体、配置、权限与任务勾选未动。

后续全套暴露此前单次运行未覆盖的 macOS orphan 组暂态 EPERM，已受控重现并做同组短暂重试/持续错误隔离修复。最新普通专项为 **52 passed / 1 skipped**，真实进程组专项十次各 **6 passed**；上述 48/47 项为修复前当次历史结果，不替代最新验证。见 [退出窗口与失败隔离](linux-display-cleanup-eperm.md)，不据此补写 Linux 容器验收。
