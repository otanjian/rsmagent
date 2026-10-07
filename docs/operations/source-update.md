# 受控源码更新

Web 更新用于配置了外部进程管理适配器的 Unix 源码部署。发布仓库/ref、目录和命令均来自部署者维护的私有文件，不能从浏览器指定。Docker、Windows 源码和桌面安装包继续使用各自部署更新方式。

## 部署配置

服务和独立更新 worker 设置 `RONGAI_SOURCE_UPDATE_CONFIG`，值为绝对路径。配置文件及 manager 必须由服务用户或 root 所有，不可被组/其他用户写入。管理目录应为 0700，独立于源码及业务数据；不能把配置放进可由 Web 修改的 `config.json`。

```json
{
  "label": "内部受信发布",
  "repository": "https://git.example.invalid/team/agent.git",
  "ref": "refs/heads/release",
  "control_root": "/srv/agent-update",
  "manager": "/usr/local/libexec/agent-source-manager",
  "requirements_lock": "requirements/production-linux-py311.lock"
}
```

示例地址是占位符，必须由部署者选择，系统没有 origin/master 或上游 CowAgent 的回退来源。Git 凭据由主机的凭据机制提供，不放进 HTTPS URL、响应或日志。依赖锁必须匹配主机 Python/平台，安装使用 `pip --require-hashes`。

`control_root/active.json` 保存当前实际目录，初次由部署者创建；服务启动命令必须遵循此文件并把其中 `data_root` 作为 `COW_DATA_DIR`。API 会核对正在服务的源码与数据根确实属于此配置。

```json
{
  "release": "/srv/agent-releases/initial",
  "data_root": "/srv/agent-data"
}
```

所有源码版本保留自己的 `.venv`，当前及候选版本均须包含 `source-update.json` 的 `cold-instance-v1` 声明。拒绝脏源码树、非快进发布、失效检查或更换过的配置。检查有效期为 10 分钟，启动及停服前重新验证目标。

## Manager 协议

可执行文件接受 `ACTION CONFIG_PATH [ARGS...]`，不经 shell 拼接。成功时退出 0 并向 stdout 输出一个 JSON 对象（`ok: true`）；失败退出非零。输出不能包含凭据或业务内容。以下是必须实现的完整协议，不能用恒定返回成功的脚本作为生产适配器。

| 动作 | 要求 |
| --- | --- |
| `probe` | 返回 `protocol: rongai-source-manager-v1`、`quarantine: true`、`independent_worker: true`，并核实这些条件真实可用 |
| `launch PYTHON WORKER OPERATION_ID` | 在业务服务执行组外启动 `PYTHON WORKER CONFIG_PATH OPERATION_ID`，立即返回；systemd 下新 session 不等于新 cgroup，必须使用独立监督单元 |
| `stop` | 停止全部 Web、调度器、渠道和子进程，关闭自动重启；部分失败也必须报错 |
| `stopped` | 重验服务组为空且自动重启已禁用，返回 `group_empty: true`、`restart_disabled: true`；不能只检查父 PID |
| `start` | 从 `active.json` 启动服务，但隔离用户流量、渠道和后台任务，允许在隔离状态执行初始化/迁移 |
| `health` | 实际请求隔离服务并验证其代码和数据身份，返回 `healthy: true`、`quarantined: true`、准确的 `release` 与 `data_root` |
| `promote` | 健康检查通过后才开放用户流量、渠道和调度 |

主机适配器必须阻止租户代码和非管理执行访问管理目录，并确保普通 Agent 不能调用 manager。manager 以及发布代码属于部署信任边界，不能由租户编辑。仓库的 `tests/fixtures/source_update_manager.py` 只管理无子任务的测试 HTTP 进程，**不是生产适配器**。

## 执行和故障处理

候选先在独立源码/venv 中安装依赖、导入自检，期间旧服务继续运行。停服后复用实例备份、完整性校验及独立目录恢复；先启动旧代码加恢复副本做恢复演练，停止后再启动候选。候选健康检查通过才调用 promote。

候选失败且尚未开放流量时，重新证明全部进程停止，恢复到新目录，再以旧代码/旧依赖启动。保留失败实例供诊断，不把快照写回仍可能有进程使用的原目录。开始 promote 后若失败，状态为 `recovery_required`，不会自动丢弃可能已产生的业务写入；需要部署者核对真实运行状态和数据再处理。

状态复用 `tmp/web-update-status.json` 文件格式/位置约定，根位于管理目录，以免切换版本丢失；原子写入并持有内核实例锁。重复 check_id 返回同一个 operation_id。worker 消失后，停服前阶段标为失败，停服后阶段标为需要恢复，不能盲目启动第二次更新。

所有检查、启动、状态接口需要实时平台管理员资格；检查和启动执行现有 Origin/CSRF 校验及审计。页面只显示固定目标 SHA 和阶段，不展示内部路径或完整子进程输出。

## 验证范围

`tests/test_fork_source_update.py` 使用本机隔离 Git 发布源、独立 venv、真实 HTTP 子进程和包含身份/凭据/外部工作区的测试数据，验证成功与恢复。这证明适配服务的协议和算法，不代表任意业务主机的 manager 已符合协议。每个部署在启用入口前仍须完成自己的服务组停止、流量隔离和恢复演练。
