# 独立 Linux 显示部署重试证据（2026-10-04）

范围为剩余 4.1 的独立 headed Chrome → Xvfb → x11vnc → websockify/noVNC 单元。没有启动本机 Chrome、SAP、OpenCode、MCP 或模型，也没有 UI/CDP 操作。没有修改 core、权限、现有配置或任务勾选。

## 原状态与资源归属

00:53:26（Asia/Shanghai）首次检查：Colima default **Stopped**，原 profile 为 aarch64 / 2 CPU / 4 GiB / 60 GiB / vz；Docker context 为 **default**。Docker CLI 29.5.2、Compose 5.1.4 可用，buildx 未安装。

00:53:51 按原 profile 执行 `colima start`，未重建、重置或修改 profile。启动后 Linux daemon 为 aarch64 / Docker 29.2.1；无运行用户容器。只读记录了既有 7 个缓存镜像、6 个已退出容器，没有启动、停止、删除或改写这些用户资源。Colima 自身正常启动时处理既有 VM 挂载、Docker socket 与上下文。

本轮唯一指定的新镜像标签是 `sap-workbench-display-fixture:20261004-arm64`。实际构建在基础层失败，没有生成该镜像，没有创建 fixture 容器、volume 或 Docker network；无需借助 prune 或删除用户资源回收。

## 实际拉取与构建

直接以既有 arm64 固定摘要拉取：

```sh
docker --context colima pull debian:bookworm-20260918-slim@sha256:0c8bbb8e987a035fe1d9704eb2e571b7e9a836e1caa46345290674b45b69e417
```

daemon 在 `https://registry-1.docker.io/v2/library/debian/manifests/sha256:0c8bbb8e987a035fe1d9704eb2e571b7e9a836e1caa46345290674b45b69e417` 的 HEAD 请求返回 **EOF**。

尝试 Docker 官方发布的 ECR Public 副本，仍固定相同原摘要：

```sh
docker --context colima pull public.ecr.aws/docker/library/debian@sha256:0c8bbb8e987a035fe1d9704eb2e571b7e9a836e1caa46345290674b45b69e417
```

该请求同样在 manifest HEAD 返回 **EOF**，未下载成功，没有据 registry 名称假定该摘要已存在或内容通过验证。官方发布关系依据 [Docker 官方 ECR 说明](https://www.docker.com/blog/news-from-aws-reinvent-docker-official-images-on-amazon-ecr-public/)；没有尝试第三方镜像或修改 Docker daemon mirror。

实际构建命令：

```sh
docker --context colima build --build-arg TARGETARCH=arm64 \
  -f Scene/sap_workbench/deployment/browser.Dockerfile \
  -t sap-workbench-display-fixture:20261004-arm64 \
  Scene/sap_workbench/deployment
```

legacy builder 实际发送 **26.62 kB** 上下文，并执行 Step 1 `ARG TARGETARCH`；Step 2 先解析 Dockerfile 的固定 amd64 基础 stage：

```text
FROM debian:bookworm-20260918-slim@sha256:f3034a6ec3c1205360777c4aae76234998866ad18806ae62b63a3f84ccad782b AS base-amd64
failed to do request: Head "https://registry-1.docker.io/v2/library/debian/manifests/sha256:f3034a6ec3c1205360777c4aae76234998866ad18806ae62b63a3f84ccad782b": EOF
```

这是基础镜像网络失败，不是 arm64 Chrome 启动失败，也不是沙箱拒绝；尚未进入 APT、归档下载、动态链接、Xvfb、Chrome 或 VNC 阶段。不能用没有运行过的组件解释失败。

## 网络读取定位

保留正常证书校验的独立 HTTPS 请求也失败：宿主机 Docker Registry（configured proxy 与单次 direct）、registry.hub.docker.com、auth.docker.io、public.ecr.aws，以及 snapshot.debian.org / deb.debian.org 均出现 `SSLEOFError`；VM 中 curl 的 Docker Registry 请求为 TLS EOF。Docker Registry 不仅 HEAD 失败，`/v2/` GET 和公开 auth token endpoint 也在 TLS 阶段失败，没有取得或输出 token。

原 Dockerfile 的 `http://snapshot.debian.org/archive/debian/20261003T000000Z/dists/bookworm/Release` 返回 **502**。`https://raw.githubusercontent.com/docker-library/official-images/master/library/debian` 可返回 200，因此不能笼统声称所有网络或 GitHub 均不可用。未改变全局代理、DNS、证书信任或 Docker daemon 配置；未禁用 TLS 校验，也未把 APT 签名/版本锁、Chrome 沙箱、非 root、只读根文件系统或能力限制取消。

## 可独立完成的真实进程证据

新增 `tests/test_sap_workbench_display_process_cleanup.py`，仅使用本次创建的临时 Python 子进程组和临时文件。验证：

1. 两个真实临时组件均由 Supervisor 停止并 reap，关闭时间小于 3 秒。
2. 临时父进程先正常退出，其后代仍在该已知进程组；Supervisor 不因父进程 poll 非空而跳过该组，后代被回收。不扫描全机 PID。
3. 实际不可读的八字节口令文件触发 PermissionError，不能在该文件状态下开始显示组件。此项在非 root 宿主执行；root 测试环境会明确跳过 DAC 证据。

```sh
.venv/bin/python -B -m pytest -q \
  tests/test_sap_workbench_display_process_cleanup.py \
  tests/test_sap_workbench_display_deployment.py
```

结果：**45 passed in 0.20s**。其中原 42 项仍为隔离下载/协议/进程替身测试，新增 3 项为实际宿主临时进程/文件验证。这些证据不代替 Linux 容器、真实 Xvfb/Chrome、RFB 认证/画面/输入或沙箱验收。

## 恢复与结论

00:58:12 执行 `colima stop`，随后恢复原 Docker **default** context；独立 `colima list/status` 确认 default profile 为 **Stopped**。只读检查没有本轮 fixture 镜像/容器残留，原用户镜像与已退出容器未操作。

源码差异只含部署文档补充、这份证据和上述独立测试；没有改锁定摘要、版本元数据或运行参数。`runtime_verified=false`、`workbench_binding_verified=false` 保留。4.1 的真实镜像/运行/输入/沙箱验收仍未完成，原因是原官方上游读取在 TLS/HTTP 阶段失败，且本轮没有可证明有效的受信联网路径；远程场景绑定亦未完成。本轮没有通过 mock 或文件存在将这两者标为完成。
