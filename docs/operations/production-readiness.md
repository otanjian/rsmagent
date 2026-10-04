# 生产修复、部署与恢复

对应 OpenSpec `harden-production-readiness`。本地实现与回归不等于生产准入；平台实测结果见 change 的 `evidence/local-apply-20261002.md`。本次没有改动实际角色、资源授权或功能开关，没有执行发布。Windows 桌面测试暂缓。

## 检查入口

```sh
bash scripts/check-production.sh --local
(
  set -eu
  candidate_id=$(mktemp)
  trap 'rm -f "$candidate_id"' EXIT
  python3 scripts/build-production-candidate.py --tag rsmagent:candidate --iidfile "$candidate_id"
  PYTHON_BIN="$(command -v python3)" bash scripts/check-production.sh --image "$(cat "$candidate_id")" \
    --deployment-env /srv/rsmagent/candidate-image.env
)
```

第一条使用 `.venv/bin/python`（可由 `PYTHON_BIN` 指定）、已安装的 Python 测试依赖与 `desktop/node_modules`，在独立临时 `COW_DATA_DIR` 运行 Python、Node、路由、模块接缝、类型和构建检查；缺依赖或回归失败返回非零。括号内步骤需要 Linux Docker 构建/运行环境；构建脚本在临时副本放入合成敏感标记，扫描所有镜像层、元数据和历史，包含后续层删除的内容，不推送镜像。构建成功且扫描通过后才输出 `--iidfile`，后续检查使用其中的不可变 image ID；即使标签被重新指向也不会验收另一个镜像。镜像冒烟检查同时验证配置用户和容器实际 UID 非 root、版本/代码树标签、实际隔离和 `/api/ready`；失败返回非零，创建后启动失败也会清理测试容器及其匿名卷。

`--deployment-env` 的父目录须已存在。只有镜像检查通过且测试容器清理完成，才原子生成 `candidate-image.env`，记录本地 image ID 的64位十六进制部分、提交和代码树摘要；失败保留旧文件，调用方必须依据非零退出码停止本次发布，不能接着使用旧文件。`--all` 中本地检查失败时也不会执行镜像检查或生成新配置。该文件只证明上述脚本覆盖的检查，不能替代真实业务、完整隔离和恢复验收。

`--local` 的原生环境探测单独输出 `PASS` 或 `NOT VERIFIED / PREREQUISITE UNMET`。本地回归通过不能抵消这个状态。当前 macOS 文件边界通过，但同用户进程环境读取未被 Seatbelt 阻断，`/api/ready` 因此返回 503。没有据此关闭 Bash、禁用网络或改为远端执行；这个环境不具备完整的生产执行隔离条件。不能通过伪造就绪值、扩大权限或跳过探测验收。Linux 必须在最终宿主/容器组合另行验证 PID namespace 等机制。

`scripts/probe-production-gaps.py --legacy-baseline` 只复现**未装配新启动器的历史工具行为**，不是当前候选检查；原工作区包缺身份库仍是其既有范围，不应将它冒充新 `--instance` 模式。

Linux 完整验收还必须覆盖启动后新建/替换的受保护文件、挂载源软链接竞争及 abstract Unix socket。当前文件遮罩只针对启动时存在的路径，且保留宿主网络 namespace；不能仅凭启动参数、现有文件探测或镜像冒烟认定这些边界通过。须在真实 Linux 环境复现并完成必要适配后才可关闭任务 2.2/2.7。

## 执行与凭据迁移

Bash 前台、后台和重试使用同一启动接缝；当前主体、Agent 绑定、资源权限、Web 会话状态在启动前重验。临时 HOME/缓存隔离，后台输出和取消按 owner/会话重验，完成后回收临时区。前后台输出在进入缓存与进度回调前统一做流式凭据脱敏，覆盖跨读取块和凭据前缀重叠；后台命令元数据同样脱敏，普通进度及时返回。Desktop 本机项目执行继续沿用原有路径及网络契约；本地后端保留合法 IP 网络与自身 Unix socket，只读提供 Python/Node 和仓库技能运行库。

每个后端进程默认最多20个正在启动或尚未完成回收的后台任务。部署方可在运行环境文件设置 `COW_BASH_MAX_RUNNING` 为正整数；0、负数和非法值回到20，不解释为关闭功能或无限容量。满载时新后台请求在创建进程前返回可重试提示，已有输出、取消和前台执行仍可用；自然完成、取消或启动失败后释放名额。此限制复用现有注册表，是单进程容量保护，不新增角色配额，不承诺限制每个命令派生的进程数；目标负载、PID和内存仍需实际容量验收。

启动器仅传递必要系统变量、作用域内业务配置及凭据。不要把旧全局业务密钥删掉后直接切换候选；应先按 [凭据映射说明](execution-credentials.md) 完成映射和正向技能验证。当前配置中存在 `dashscope_api_key`，只记录键名，尚未将真实凭据或角色写入测试库。原模型调用继续由现有模型服务处理；脚本使用的副本需要明确租户、Agent、用户和用途，不能将部署主密钥作为技能变量。

## Linux 部署

唯一维护的构建定义为 `docker/Dockerfile.latest`，根 `Dockerfile` 指向它。Python 3.11 与 Node 22 基础镜像按官方多架构摘要固定；amd64/arm64 各有包含哈希的 Linux 依赖锁。Debian 常规与安全源保留，Playwright Chromium、语音、渠道及文档依赖保留。更新锁的命令写在锁文件头；应重新扫描并重建，不能使用 macOS `pip freeze` 替代。

构建脚本输出 HEAD、临时构建副本内容摘要、镜像名及不可变 image ID；正式验收再保存 RepoDigest。摘要读取已复制的文件字节及软链接目标文本，随后才放入合成排除标记，避免复制后编辑源文件导致摘要与构建内容不符。未提交工作树不能仅用 HEAD 冒充固定候选。部署前将以下实例配置放在受控的 `/srv/rsmagent/compose.env` 中（路径为目标宿主示例，勿填入源码）；镜像ID和版本信息单独使用检查程序生成的 `candidate-image.env`，不要填写可变标签：

```dotenv
RSMAGENT_ENV_FILE=/srv/rsmagent/runtime.env
RSMAGENT_DATA_DIR=/srv/rsmagent/data
RSMAGENT_WORKSPACE_DIR=/srv/rsmagent/workspace
RSMAGENT_PORT=9899
RSMAGENT_CPUS=2.0
RSMAGENT_MEMORY=4g
RSMAGENT_PIDS=512
```

数据目录和工作区预先由 uid/gid 10001 可写；不要对既有未知目录递归 chown。运行环境文件 0600，存放部署主密钥，单独备份托管。其他租户/Agent/身份库/业务文件若配置在这两个根之外，必须逐一加持久化挂载，并核对实例清单。渠道在 home 外存储的游标/登录文件也必须纳入持久化范围；不能用仅挂载默认工作区证明完整持久化。

完成全部必需验收后，在目标环境执行：

```sh
env -u RSMAGENT_IMAGE_ID docker compose \
  --env-file /srv/rsmagent/compose.env --env-file /srv/rsmagent/candidate-image.env \
  -f docker/docker-compose.yml up -d --no-build --pull never
```

清除同名 shell 变量，防止它覆盖检查生成的镜像ID。Compose 只引用 `sha256:<image ID>`，没有构建入口且禁止拉取；镜像不在目标 Docker daemon 中时失败，不退回标签。当前流程使用本地 image ID，不是 registry 的 manifest digest：跨主机需传输同一镜像（如 `docker save/load`），再在目标 Linux daemon 上对该 ID 执行检查并生成配置，不能把 RepoDigest 直接填进 `RSMAGENT_IMAGE_ID`。旧 `RSMAGENT_IMAGE` 变量不再使用，构建统一由 `build-production-candidate.py` 完成。容器内监听 `0.0.0.0:9899`，宿主仅绑定回环端口。2 CPU/4 GiB/512 PID 是待容量实测的起始值，不是已验证容量承诺。

Linux 需支持非 root bubblewrap 的 user/PID/IPC/UTS namespace；当前不添加 `privileged`、整根挂载、Docker socket 或全局 `seccomp:unconfined`。实际 Docker 默认 seccomp、内核用户命名空间或 LSM 不允许时，发布验收失败；在目标环境定位所需最小配置后再复测，不将失败说成通过。

## HTTPS、就绪与故障定位

`docker/nginx.conf.example` 提供宿主 Nginx TLS 示例；替换域名/证书，覆盖 Host、X-Forwarded-For、X-Forwarded-Proto、X-Forwarded-Host，避免拼接客户端伪造链。配置 `trusted_proxies` 为后端实际看到的代理对端 IP 的精确列表；容器中它可能是网桥地址，不能机械填写 127.0.0.1 或信任任意来源。限制后端端口仅代理可达。

可信 TLS 终止时，登录 Cookie 为 Secure/HttpOnly/SameSite=Lax；来源校验同时比较 scheme 与 host。单元测试验证可信/不可信来源，真实 TLS 与远程断线重连仍需目标部署验证。

`/api/health` 保持桌面存活契约。`/api/ready` 检查只读身份库查询、必要目录访问与缓存的启动隔离实测，仅返回布尔分类，不迁移数据、不调用模型。状态异常时依次检查容器退出原因、挂载属主/磁盘空间、数据库读取、原生隔离前置。修复内核/隔离配置后需重新启动以刷新缓存探测。

Compose 的 `restart: unless-stopped` 处理进程退出；**unhealthy 本身不会使 Docker 自动重启**。磁盘/内存可用 `df -h`、`docker stats --no-stream`、`docker inspect` 查看。先记录实际负载/排队/延迟，再调整资源值；不要预设分布式服务或额外队列。

运行日志默认 20 MiB × 5 个历史文件，环境变量 `COW_LOG_MAX_BYTES`、`COW_LOG_BACKUP_COUNT` 可调整为正整数；0/负数/非法值回到有界默认值。日志位置保持现有 `COW_DATA_DIR/run.log` 语义，新文件0600，已有模式在轮转后保留。文件写失败向 stderr 报脱敏诊断，控制台继续输出；审计表保留语义不变。

`http_policy_gate_fail_closed=false` 不再恢复身份异常放行，可删除旧键。异常统一503，无 handler 副作用；确定性400/401/403及恢复后的合法请求保持。限流容量到期自动回收，无需新增缓存服务。

## 整实例冷备份与恢复

旧 `cow backup` / `cow restore` 仍是工作区格式及原默认行为。新增显式 `--instance`，不能混用两种格式。整实例读取配置、只读身份库登记、全部 Agent roster 与外置根，保存角色/用户/租户、历史、文件、调度数据和加密凭据；保留内部软链接和空目录，拒绝指向登记根之外的软链接与活动 socket。身份库在停机锁内通过 SQLite backup API 生成独立快照，包含崩溃后仍在 WAL 中的已提交页；不修改源库，也不把原 WAL/SHM 再叠加到快照上。其他业务数据库随其完整登记目录保存。外置业务根必须存在且可读；不自动初始化缺失数据库或跳过损坏 roster。

原生备份命令应使用与实例启动相同的 HOME、工作目录及运行配置环境。声明过的配置环境覆盖值会合入受控归档中的配置；容器模式读取目标容器的环境与工作目录，不使用操作者的配置覆盖值。主密钥仍由操作者单独提供。未声明的宿主环境变量不归档；部署环境文件仍需按现有密钥保管流程另行管理。清单记录应用版本，配置路径在恢复时重定位。

微信凭据和企业微信客服游标包括默认路径；微信各实例的凭据文件保留与基础文件的命名关系，即使基础文件尚未生成也可恢复。独立外置文件按明确文件列表归档，不扩大到整个 HOME。尚未登录的渠道路径同样重定位到恢复根，不写回旧实例。

必须证明所有写进程停止。主 PID 消失、HTTP 不通、父进程锁释放都不足以证明独立后台任务退出。Linux 原生部署可使用专用 cgroup 的空组证明；本机无专用运行管理器时仅接受上次启动标记来自前一次系统启动的证据。不要手工伪造 `.instance-active.json`。旧版本没有标记的原生实例需先受控运行本版本，再停服务/整机重启；同次启动下仅 `cow stop` 不足以完成原生冷备。

Docker 部署可在**Linux Docker 宿主**使用已停止的精确容器 ID，Docker daemon 提供停止/挂载证明，程序前后重验并与应用启动共用数据根互斥锁。先 `docker compose stop`，保留容器（不要先 `down`），在维护窗口禁止外部管理器/人工重新启动；其他写相同卷的进程也必须停止。无需在 Agent 内挂 Docker socket。

```sh
# 在 Docker 宿主；runtime 环境通过受控方式提供主密钥，不放命令行。
COW_DATA_DIR=/srv/rsmagent/data cow backup --instance \
  --container <stopped-container-id> -o /secure-backups/instance.zip
cow restore /secure-backups/instance.zip --instance --destination /srv/rsmagent/restored-20261002
```

归档临时写入，校验内容并再次确认停机状态后才原子替换，失败保留旧备份。目录0700/归档0600；备份可能含业务明文，放受控加密存储。`.env` 中的 `COW_CREDENTIAL_MASTER_KEY` 被排除，其他凭据仍保留；发现该主密钥混入其他文件则拒绝生成包。主密钥由部署现有密钥保管方式单独托管，恢复时提供原值；缺失或错误不能报成功。支持的数据库 schema 必须与恢复代码一致，不自动猜测跨版本迁移。

恢复目标必须不存在，在同文件系统暂存目录中先验证清单、哈希、路径、数据库及凭据解密，然后重写逻辑根路径、原子发布。用户/租户/owner ID 保持，输出 `restore-map.json`，不启动服务、不触发真实调度/外发。先检查根映射、挂载和配置，并更新启动环境中的路径覆盖值，避免覆盖新配置而重新指向旧根；全局环境文件若位于独立逻辑根，需要按映射重新安装到受控运行环境位置。再用合成外部接收端检查登录、授权、文件、历史、调度与凭据，测恢复时长，最后按明确部署指令切换。

失败或回滚时停止候选、保存诊断，用已验收且 schema 兼容的镜像/快照恢复；不要自动退回已知越权执行实现。真实 Docker 停机/恢复、外部业务系统与容量验证当前尚无目标环境证据。

## CI 与依赖处置

`.gitea/workflows/production-readiness.yml` 为实际交付仓库的 `main`/`rdai` 配置同一检查入口，runner 标签为 `macos-production`、`linux-production`。文件存在不代表 runner 已上线或分支 required checks 已启用；需仓库端真实运行及准入结果补证，Windows 不在本轮必需平台中。

依赖扫描已修复锁内 aiohttp/PyPDF2 报告及 websocket-client 约束冲突，PDF 回退改用 pypdf 并跑实际文本提取。仍保留 Click 8.1.8 的 `PYSEC-2026-2132` 报告：固定版为8.3.3，但现有 gTTS2.5.4 约束 Click<8.2。仓库没有 `click.edit` 调用；当前受影响入口不可达，保留语音功能与此项处置记录，不忽略扫描结果、不强行破坏依赖约束。新增 `click.edit` 使用或 gTTS 支持升级时必须重新评估。镜像系统包扫描与干净重建仍需 Linux 环境。
