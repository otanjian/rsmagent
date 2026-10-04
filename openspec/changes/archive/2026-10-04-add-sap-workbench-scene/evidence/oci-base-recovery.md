# 固定 Debian 基础镜像恢复

2026-10-04 新增场景独立工具 [fetch-debian-oci.py](../../../../../Scene/sap_workbench/deployment/fetch-debian-oci.py)，解决原基础镜像 Registry HEAD 返回 EOF 时的下载路径。实际下载、Docker 导入和构建结果见 [结构化记录](oci-base-recovery.json)。它不是主服务运行依赖，未修改 Dockerfile、兼容锁、普通 Agent、桌面主进程或 rsmcode 源码。

## 已完成

- 从 [Debian 官方镜像构建产物](https://github.com/debuerreotype/docker-debian-artifacts) 的两个固定提交下载 amd64、arm64 原 OCI 元数据与 rootfs，校验原 manifest/config/layer 的 SHA-256、大小、平台及解压流 diff_id。两个 manifest 与现有 `browser-versions.json` 完全一致；不换版本或可变 tag。只生成 OCI 归档，不解包 rootfs、不执行镜像内容。
- 本项目 Python 3.14.3 实际运行新 CLI，两个归档成功输出；随后独立读取归档，确认五个普通文件成员和全部原 payload 字节与已导入镜像一致。HTTPS 使用正常 CA 校验。MCP 的 Python 3.10 环境默认 CA 数量为零，其独立尝试据实失败；更新后的诊断为 `TLS certificate validation failed`，临时输出目录回收，没有关闭校验。
- 两份锁定镜像均通过 `docker image load`，原摘要引用可查询。沿原 arm64 Colima profile 构建原 Dockerfile，基础 stage 已通过，推进到 Step 6 的 APT。第一次返回 100，用时 9.26 秒；只在构建进程中沿用已有 VM 代理后，main/security InRelease 均返回 502，返回 100，用时 0.51 秒。APT 的“not signed”附带消息来自未取得 InRelease，不能据此断言官方签名有问题。
- CLI 与部署/退出清理原组合 **90 passed，0.45 秒**，其中 CLI 38 项，专项不累加。失败测试覆盖元数据/层校验、总时间和大小边界、重定向拒绝、原文件保护、失败回收与固定错误提示；其他测试保持原部署/清理覆盖。这是隔离回归，没有运行 Chrome、Xvfb 或 VNC 认证/画面/输入。

## 构建后的状态

本次使用 `--force-rm` 回收构建容器，并只删除自身生成的 ARG 中间镜像，保留两个已验证的基础镜像缓存。原六个镜像 ID 和六个已退出容器均保留，没有运行容器或本次 fixture 成品镜像。Colima 已恢复原停机状态，Docker context 恢复 `default`；没有修改全局代理、DNS、CA、APT 签名校验或浏览器沙箱。

主 Web、桌面、OpenCode 和两个 MCP 服务没有因这个独立工具重启，上一轮已加载的 15 份运行源码摘要保持一致。本轮没有新的 SAP/OpenCode 页面、模型或业务 MCP 调用。用户暂停的实际操作检查继续暂缓，保存/过账/删除保持关闭。

## 仍未完成

APT snapshot 无法读取，Linux 完整镜像尚未构建，`runtime_verified`、`workbench_binding_verified` 仍为 false。Docker Hub 直接拉取未通过；官方 OCI 下载与导入已通过，不能把当前阻断继续写成基础镜像不可取得。构建用 main/security 快照保持原 `20261003T000000Z`，没有以其他仓库或未签名包替代。

本轮只读复查历史 SAP 页面证据，未找到页签/分页的原生事件与完整回读基线，也没有真实保存回执及完整业务核验合同；不据替身补造这些接口。统一登录、生产权限/计量、实际双用户/桌面/负载/提交验收保留原延后范围。完整计划仍为 **42/57，剩余 15 项**；没有新增整项勾选或归档。

复现命令和固定来源见 [Linux 部署说明](../../../../../Scene/sap_workbench/deployment/BROWSER.md#固定基础镜像的独立下载与导入)。原 MCP/DOM 全套 **1182 pass / 2 skip、Node 170 pass** 是 [上一轮](mcp-dom-closeout.md) 的结果，本轮未重跑全套、Node、Bun 或联网组件专项。
