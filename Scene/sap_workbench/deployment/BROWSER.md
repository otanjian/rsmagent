# 独立 Linux 浏览器显示单元

此目录新增一个可选部署单元：有头 Chrome → Xvfb 的独立 X 显示 → x11vnc 的 RFB → websockify 的 WebSocket → noVNC。它使用真实 VNC 传输；现有 macOS 工作台的 CDP 截屏桥继续按原方式运行，主服务不会自动启动或依赖这个容器。

`browser-versions.json` 记录这套 Linux 单元的独立期望值。2026-10-03 已核对上游资源和下载校验，**尚未成功构建或启动镜像**；`runtime_verified` 和 `workbench_binding_verified` 均为 `false`。2026-10-04 按原合同复核，任务 4.1 的独立组件提供、版本固定及主服务不依赖浏览器在线已完成；依据是完整部署链路、版本/下载锁和隔离证据，而非仅有文件。实际容器、远端场景及 4.3/4.4/4.7 运行验收仍未通过，远程显示未登记为可用节点。本轮全套、进程组修复与主后端重载见 [本轮合同收尾](../../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/next-contract-closeout.md)；主后端重载未启动该显示单元，原构建失败记录保留。

| 组件 | 固定值 | 依据与范围 |
| --- | --- | --- |
| Debian | bookworm-20260918-slim / 12.15 | amd64、arm64 各自固定 OCI manifest digest；官方 OCI 完整下载及 Docker 导入通过，Registry 直接拉取尚未通过 |
| Debian 软件源 | 20261003T000000Z | 固定 main / security snapshot，保留 APT 签名与包哈希验证；实际解析尚未验证 |
| Xvfb | 2:21.1.7-3+deb12u13 | Debian bookworm 官方包页面，构建时要求精确版本 |
| x11vnc | 0.9.16-9 | 同上 |
| Chrome for Testing | 154.0.8037.92 | 官方 Stable Linux amd64 / arm64 下载元数据；固定对象 generation、大小和 Google 提供的 MD5 |
| noVNC | 1.7.0 | 官方 tag 源码归档，下载并核对 SHA-256，已检查 `vnc.html` / `core/rfb.js` |
| websockify | 0.13.0 | 官方 tag 源码归档，下载并核对 SHA-256；实际 `run --help` 检查通过，无监听进程 |
| Chrome seccomp | Playwright v1.56.0 上游模板 | 原样保存 SHA-256；允许 Chrome 非 root 沙箱需要的用户命名空间调用，并非引入 Playwright 控制器 |

Linux Chrome 154.0.8037.92 与历史 macOS 154.0.8037.95 是两套来源。此文件不升级旧兼容期望值、不修改系统 Chrome、不接管系统自动更新；新单元需要自己的运行验收。

## 构建与启动

以下命令从本项目根目录执行，需要已经可用的 Linux Docker daemon、Docker Compose 和对上游镜像/软件源/归档的正常访问。部署程序不会安装或启动 Docker/Colima，也不会停用已有本机服务。

```sh
docker build --build-arg TARGETARCH=arm64 \
  -f Scene/sap_workbench/deployment/browser.Dockerfile \
  -t sap-workbench-display:154.0.8037.92-novnc1.7.0 \
  Scene/sap_workbench/deployment
```

示例对应本机 Linux arm64 Docker daemon；x86_64 daemon 使用 `--build-arg TARGETARCH=amd64`。BuildKit 自动提供目标平台的 `TARGETARCH`，legacy builder 没有这个自动参数，必须显式传入，且与 Docker 的目标平台一致。Dockerfile 选择已锁定的 amd64 或 arm64 资源，其他架构拒绝；legacy builder 可能先解析/拉取两个基础 stage，不能把未选中的基础层误认为发布架构。Compose 的构建路径要求可用的 BuildKit/buildx；缺少 buildx 时可以先按上面的显式架构命令构建，再用 `compose up -d --wait --no-build` 启动已构建镜像，不自动安装 buildx。

源码归档使用 HTTPS 与 SHA-256；Chrome 使用 HTTPS、固定对象 generation、大小与官方 MD5 做传输完整性检查，未声称拥有该 Chrome 归档的独立 SHA-256 锁。镜像的 Debian 最小环境先通过 HTTP snapshot 引导安装 CA，仍要求签名可信的 Release 与包哈希；只关闭历史 Release 有效期检查，没有 `trusted=yes` 或允许未签名包。

为显示会话创建独立 VNC 口令文件。它与 SAP/MCP 登录凭据无关；不要将密码写入命令、URL、仓库或环境文件。经典 VNC 认证使用前八个字符，这个口令文件只是 VNC 协议需要的格式，不能作为安全密码库。

```sh
umask 077
mkdir -p /tmp/sap-workbench-display-secret
chmod 700 /tmp/sap-workbench-display-secret
docker run --rm -it --user 0:0 \
  -v /tmp/sap-workbench-display-secret:/secrets \
  --entrypoint x11vnc \
  sap-workbench-display:154.0.8037.92-novnc1.7.0 \
  -storepasswd /secrets/vnc-password
chmod 644 /tmp/sap-workbench-display-secret/vnc-password
export SAP_WORKBENCH_VNC_SECRET_FILE=/tmp/sap-workbench-display-secret/vnc-password
docker compose -f Scene/sap_workbench/deployment/browser-compose.yaml config
docker compose -f Scene/sap_workbench/deployment/browser-compose.yaml up -d --wait
```

`-storepasswd` 的单一参数是输出文件路径，x11vnc 在终端提示输入口令。宿主目录 `0700` 保证其他宿主用户不能遍历；目录内 `0644` 文件允许容器的非 root UID 10001 读取挂载的八字节文件。不要将这个文件移到可公开遍历的目录。启动时缺失、不可读或不是八字节的文件直接失败，不会降级到无密码 VNC。

打开 [本机 noVNC](http://127.0.0.1:6080/vnc.html?autoconnect=1&resize=scale)，输入独立 VNC 口令。初始页面只能是 `about:blank`，容器不会自动访问 SAP、登录、复用本机 Chrome cookie 或调用模型/MCP。网页证书验证与 Chrome 沙箱均保持开启。

```sh
docker compose -f Scene/sap_workbench/deployment/browser-compose.yaml ps
docker compose -f Scene/sap_workbench/deployment/browser-compose.yaml logs --tail 50
docker compose -f Scene/sap_workbench/deployment/browser-compose.yaml exec -T browser \
  python3 -B /opt/sap-browser/healthcheck.py
```

健康检查要求四个进程存活、Chrome 报告精确版本、x11vnc 发出完整 RFB banner、noVNC HTML 可读，以及 WebSocket 确实桥接该 RFB banner。检查在 VNC 认证前结束，不发送页面输入。它没有验证浏览器画面解码、键鼠操作、SAP 登录或工作台绑定；这些仍需要容器运行后单独验收。

## 网络、沙箱和关闭

Compose 默认只发布 `127.0.0.1:6080`。RFB 5900 与 Chrome CDP 9222 仅在容器内部 loopback 监听；它们不作为外部工作台接入地址。容器运行为 UID/GID 10001，根文件系统只读，使用独立 tmpfs profile、丢弃 Linux capabilities、保留 `no-new-privileges` 和已校验的 seccomp。没有 `--no-sandbox`、`--ignore-certificate-errors`、宿主网络、Docker socket 或源码目录挂载。

目标 Docker 内核需要允许 Chrome 沙箱使用的用户命名空间；不支持时启动失败，部署脚本不会修改宿主内核设置或关闭沙箱。CDP 只用于容器内组件准备检查，这个文件单元没有开放任何业务执行 API。

远程查看可使用 SSH 转发，仍保留服务节点的 loopback 发布：

```sh
ssh -N -L 6080:127.0.0.1:6080 user@browser-node
```

需要同一节点多会话时，每个会话必须有独立容器、显示、profile、口令和端口；Compose 可用独立 `-p` 项目名及 `SAP_WORKBENCH_NOVNC_PORT`。当前单元没有实现平台按用户创建/回收容器、鉴权网关、HTTP/WebSocket 反向代理或 SAP 会话归属校验，不能直接将 noVNC URL 填入现有 `browser_service_ref` 冒充场景执行节点。远程节点与工作台会话适配仍待实现和验收。

停止仅本部署项目：

```sh
docker compose -f Scene/sap_workbench/deployment/browser-compose.yaml down
```

监督进程接到终止信号后按反序停止 websockify、x11vnc、Chrome 与 Xvfb 的进程组，超时后强制清理本单元子进程。容器删除后 tmpfs 中的 profile、cookie 和进程状态消失；外部 VNC 口令文件保留，按需要由其所有者删除。该命令不停止工作台前后端、桌面端、OpenCode 或 MCP 服务。

## 当前验证结果与缺口

2026-10-03 首次检查本机 Docker CLI 29.5.2、Compose 5.1.4 可执行，Colima 未运行，`~/.colima/default/docker.sock` 不存在。随后根代理沿已有 Colima profile 启动 Linux arm64 Docker daemon（既有 2 CPU / 4 GB / vz 配置，未重置或修改 profile），确认没有其他容器，并尝试实际构建。本机未装 buildx，`--progress` 不受支持；改用 legacy `docker build --build-arg TARGETARCH=arm64` 后确实开始解析 Dockerfile，但首个固定 Debian 镜像的 Docker Hub HEAD 请求返回 EOF，构建没有进入 APT 或 Chrome 阶段。根代理随后恢复原停机状态；23:46:05 的独立只读 `colima status` 已确认未运行。本子任务没有另起 VM 或容器。

该阶段构建阻断是上游镜像网络读取失败，不能继续将“daemon 从未可用”作为结论。Docker Registry / Debian snapshot 的直接读取也出现 TLS EOF；没有绕过 TLS、删除锁定版本或替换未经验证的镜像源。2026-10-04 后续已通过官方 OCI 下载/导入推进到 APT，最新状态见下方恢复记录。

本次已完成 Compose 配置解析、上游 noVNC/websockify 的实际下载、校验与解包，以及 websockify CLI 参数检查。`tests/test_sap_workbench_display_deployment.py` 的 **42 项隔离测试通过**，覆盖端口边界、下载完整性、归档路径/符号链接、口令格式及非 root 读权限、启动失败、进程关闭与 RFB/WebSocket 检查；测试使用进程和网络替身。实际镜像构建尝试在基础镜像 HEAD 阶段失败，没有成功生成镜像、启动 Chrome/Xvfb/VNC、查看画面、发送真实键鼠输入或连接 SAP/OpenCode/MCP。

最后只读审查完整下载了官方 arm64 Chrome 归档（196,517,840 字节），固定 Google MD5 核验通过；ZIP 中实际 `chrome` / `chrome_crashpad_handler` 路径存在，没有符号链接条目。只解析两个 ELF 的动态库与版本需求，没有执行二进制；Chrome 的最高 GLIBC 需求为 2.25，crashpad 为 2.17，低于 [Debian 12 libc 的 2.36](https://packages.debian.org/bookworm/libc6)。直接依赖 `libudev.so.1` 与 `libexpat.so.1` 已分别由现有包链 [GTK3](https://packages.debian.org/bookworm/libgtk-3-0) → [libcolord2](https://packages.debian.org/bookworm/libcolord2) → libudev1，以及 [libgbm1](https://packages.debian.org/bookworm/libgbm1) → libexpat1 覆盖；其余直接共享库也对应现有显式包或基础 libc。没有据静态 ELF 检查声称动态链接、dlopen、两个架构或 Chrome 沙箱运行通过，下载临时文件已清理。

仍未证明：Registry 直接拉取与 APT snapshot 解析、Chrome 的两个 Linux 架构运行、容器沙箱可用性、完整 VNC 认证/画面/输入、显示容器实际关闭、远程工作台接入与多用户回收。官方 OCI 下载和导入已通过；保留其他失败证据和未验收状态，不能把静态配置与替身测试解释成运行通过。

## 固定基础镜像的独立下载与导入

当 Registry HEAD 失败而 GitHub raw 可达时，可运行场景独立 CLI。它读取现有 `browser-versions.json` 的两架构 manifest 锁，从 [官方构建产物](https://github.com/debuerreotype/docker-debian-artifacts) 的不可变提交下载原 OCI 元数据和 `blobs/rootfs.tar.gz`，核对 manifest/config/layer、大小、平台及解压流 diff_id。raw GitHub 的 `blobs/sha256/<digest>` 是符号链接文本，不能作为实际 layer。该工具不调用 Docker、不解包 rootfs、不启动浏览器，原 Dockerfile 和锁定值不变。

```sh
.venv/bin/python -B Scene/sap_workbench/deployment/fetch-debian-oci.py \
  --arch all --output /tmp/sap-workbench-debian-oci-new
docker image load --input /tmp/sap-workbench-debian-oci-new/amd64.tar
docker image load --input /tmp/sap-workbench-debian-oci-new/arm64.tar
docker build --force-rm --build-arg TARGETARCH=arm64 \
  -f Scene/sap_workbench/deployment/browser.Dockerfile \
  -t sap-workbench-display:154.0.8037.92-novnc1.7.0 \
  Scene/sap_workbench/deployment
```

输出目录必须不存在，父目录必须存在；工具在全部请求架构验证成功后才发布归档，不覆盖旧文件，失败时清理自身临时内容。HTTPS 使用 Python 的正常 CA 校验，禁止重定向；元数据 128 KiB、压缩层 40 MiB、解压流 150 MiB、socket 15 秒、整个获取流程 360 秒上限。推荐用本项目的 Python 环境；本机 MCP Python 3.10 缺少默认 CA 的实际尝试失败，不通过关闭校验解决。

2026-10-04 两种架构的完整官方 OCI 已下载、校验并成功导入，原摘要引用可查询。原 arm64 构建已通过两个基础 stage，现停在 APT：原路径无法连接，沿用已有 VM 代理的单次构建中 main/security InRelease 均为 502。没有成品镜像或显示运行验收。构建容器和自身 ARG 缓存已清理，两个验证过的基础镜像缓存保留，原镜像/容器保留，Colima 恢复停机、Docker context 恢复 default。新 CLI 与部署/清理组合 **90 项通过**，详情见 [基础镜像恢复记录](../../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/oci-base-recovery.md)。

## 上游来源与许可

- [Chrome for Testing 官方版本/下载清单](https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json)。
- [Debian 官方镜像目录](https://github.com/docker-library/official-images/blob/master/library/debian)、[Xvfb 包](https://packages.debian.org/bookworm/xvfb)、[x11vnc 包](https://packages.debian.org/bookworm/x11vnc)。基础 manifest 来自 `browser-versions.json` 中所锁定版本的官方 OCI 元数据。
- [noVNC v1.7.0](https://github.com/novnc/noVNC/releases/tag/v1.7.0)，MPL-2.0；归档保留上游许可文件。
- [websockify v0.13.0](https://github.com/novnc/websockify/releases/tag/v0.13.0)，LGPL-3.0；归档保留上游许可文件。
- [Playwright Docker 沙箱说明](https://playwright.dev/docs/docker)；`chrome-seccomp.json` 原样来自 [v1.56.0 官方模板](https://github.com/microsoft/playwright/blob/v1.56.0/utils/docker/seccomp_profile.json)，受 [Apache-2.0 许可](https://github.com/microsoft/playwright/blob/v1.56.0/LICENSE) 约束。
- [x11vnc 官方口令命令说明源码](https://github.com/LibVNC/x11vnc/blob/master/src/help.c)。

## 2026-10-04 实际构建重试

沿原 Colima default profile（aarch64、2 CPU、4 GiB）再次启动 daemon，保留原用户镜像与已退出容器，没有启动这些用户容器。显式 arm64 的实际 `docker build` 再次发送上下文，但 legacy builder 在 Step 2 解析首个固定 amd64 stage 时收到 Docker Hub manifest HEAD EOF。直接拉取原 arm64 摘要也失败；Docker 官方 ECR Public 副本以同一原摘要请求，亦收到 HEAD EOF。没有把可变 tag、第三方镜像或其他 Debian 版本代入构建。

宿主与 VM 的 Docker Registry/auth HTTPS，以及 Debian snapshot HTTPS 的独立请求同样返回 TLS EOF，snapshot 的原 HTTP 引导路径返回 502；GitHub raw 可读。没有改全局代理、DNS、CA、TLS 校验或沙箱参数，也没有因失败删除既有锁定值。构建未进入 APT 或 Chrome 阶段，没有生成本轮 fixture 镜像或容器。

补充的真实临时 Python 进程组专项验证了监督器停止自身组件、父进程先退出后仍回收其后代进程组，以及实际不可读口令文件在组件启动前拒绝。结合原 42 项隔离专项，**45 项通过**。这三项是宿主上的临时进程/文件证据，不是 Chrome、Linux 容器、画面/键鼠或沙箱运行通过；`runtime_verified` 与 `workbench_binding_verified` 仍为 `false`。

测试结束恢复了 Colima 的原停机状态和 Docker 的原 `default` context。本轮没有 SAP/OpenCode/MCP/模型访问，也没有 UI/CDP 操作。详细命令、失败范围及恢复状态见 [独立重试证据](../../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/linux-display-retry.md)。

## 2026-10-04 独立入口与源码核验

已进一步核对 Dockerfile/Compose 的固定架构来源、非 root 身份、只读根文件系统、tmpfs profile、独立 secret、Xauthority、进程启动顺序、健康检查与主服务的独立性。修复了停止竞态：ready 检查期间已请求停止，即使该检查返回 true，监督器也拒绝成功推进或补启下一组件；真实临时 Python 组件测试确认只有已经启动的首个进程存在且被回收。

另以固定官方 SHA-256 实际下载并解包 noVNC 1.7.0（726,728 字节）与 websockify 0.13.0（57,826 字节），在宿主 Python 3.14.3 启动它们的真实 shell/模块入口。测试私有 loopback 端口提供 `vnc.html`、`app/ui.js` 和 `core/rfb.js`，原 RFB/noVNC/WebSocket 健康函数通过，双向 binary WebSocket 确认只传输本次 fixture 的 RFB banner/回显字节。它没有运行真实 RFB 认证、浏览器键鼠或 UI。退出后本次 process group、端口、线程和下载临时目录均回收。

```sh
SAP_DISPLAY_UPSTREAM_SMOKE=1 .venv/bin/python -B -m pytest -q \
  tests/test_sap_workbench_display_process_cleanup.py \
  tests/test_sap_workbench_display_deployment.py \
  tests/test_sap_workbench_display_upstream.py
```

上述显式联网组件专项合计 **48 项通过**。普通回归不设置开关时，联网专项跳过，其他 **47 项通过**；主服务不会 import 或自动启动这个测试组件。资源提供、部署入口及兼容锁已具备代码与真实上游桥接证据；Linux 镜像完整构建、Chrome/Xvfb/沙箱、真实画面/输入及场景绑定仍没有运行验收，`runtime_verified` 和 `workbench_binding_verified` 保持 false。详细合同边界见 [源码与入口核验](../../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/linux-display-source-audit.md)。

随后全套回归发现 macOS 在已退出父进程的后代终止时，原 process group 短暂返回 EPERM。三组真实临时 fixture 复现后，监督器改为仅对自身登记组做最多 40ms 的同信号重试；持续 EPERM 仍是失败，所有其他组继续清理后才固定报错。测试的 finally 也等待该组实际 ESRCH，不把 EPERM 当成已消失。四组件新增重试等待最多 0.32s，源码受控等待预算约 12.35s，保留现有 15s Compose grace；这不是对所有 OS 调度/syscall 的 wall-clock 保证。

修复后普通部署专项为 **52 passed / 1 skipped**；真实临时进程组文件另在十个独立子解释器中各 **6 passed**。本轮未重跑联网组件、启动容器或浏览器，也没有改变未验收标记。失败、精确 fixture PID 观察与边界见 [退出窗口与失败隔离证据](../../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/linux-display-cleanup-eperm.md)。
