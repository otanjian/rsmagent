# 独立显示监督器：macOS 进程组退出窗口与失败隔离

2026-10-04，宿主 macOS 26.4、Python 3.14.3、UID 501。本轮只启动真实临时 Python fixture，不访问 SAP、OpenCode、MCP 或模型，不启动浏览器、不重试镜像请求、不改 Colima、代理、证书或核心程序。

## 失败与受控重现

全套回归中 `test_real_supervisor_reclaims_group_after_temporary_parent_exited` 曾在父进程 `wait()` 已返回 0 后，向原 process group 发送 SIGKILL 时收到 EPERM。再次单独运行同文件为 **1 failed / 4 passed**：这次 `Supervisor.stop()` 已返回，但紧接着的 group 查询与 finally SIGKILL 收到 EPERM。此前单次专项通过不能证明该窗口不存在。

使用三组新临时父/子进程逐组重现。父进程以 `start_new_session=True` 启动，记录其直接后代 PID 后自然退出；只读取本次记录的两个 PID，不扫描其他进程。TERM 前的精确 `ps -p <parent>,<child> -o pid=,ppid=,pgid=,uid=,stat=,comm=` 结果为：

| 本次父进程/PGID | 本次 child | TERM 前 PPID | UID | 状态 |
| --- | --- | --- | --- | --- |
| 63906 | 63907 | 1 | 501 | S |
| 63917 | 63918 | 1 | 501 | S |
| 63926 | 63927 | 1 | 501 | S |

三次 TERM 后，立即 `killpg(original_group, 0)` 均返回 EPERM；紧接着的精确 PID 查询已没有进程行，同一组的 SIGKILL/后续查询均为 ESRCH。该证据证明本机存在短暂的退出组查询/信号窗口；未捕获 Z 状态，不把它写成已证明的 zombie 机制，也不推广为 Linux 行为。此次与失败测试记录的临时 descendant 均已退出，没有遗留 fixture 活进程。

## 最小修复与保守失败语义

`Scene/sap_workbench/deployment/entrypoint.py` 的 `_signal_group()` 仅对已登记的原 process group 操作。若收到 PermissionError，最多做五次尝试、四次 10ms 等待；只有后续信号实际成功或 ESRCH 才返回。持续 EPERM 与其他 OSError 保留为失败，不把 EPERM 解释为资源已释放。

`stop()` 首先设置 stopped，拒绝下一次组件启动；所有已登记组都尝试 TERM、等待、KILL、等待。一个组的信号/等待错误不会跳过其他组；全部尝试后以固定消息 `display process group cleanup failed` 抛出 OSError，并保留最早原因。CLI 现有 OSError 边界使退出为非零、输出固定配置失败消息，不打印命令行/环境。调用方不能将这一错误当成清理成功。

四组件的新增重试等待最多 **0.32s**；原共享 TERM grace 为 8s，原四次 KILL 后 wait 最多各 1s，并有最多三次已过 deadline 的 10ms 最小 wait。按这些 timeout/sleep 的源码预算最多约 **12.35s**，仍位于既有 Compose `stop_grace_period: 15s` 内。该数字是受控等待预算，不是对操作系统调度或不可中断 syscall 的绝对 wall-clock 保证；没有延长容器 grace 或放弃沙箱。

真实 orphan 测试的检查与 finally 都等待该精确组实际 ESRCH；暂态 EPERM 只继续轮询，三秒仍没有 ESRCH 就失败。持续 EPERM 用隔离注入证明，不声称已在 Linux 产生真实权限错误；另一个已登记的真实 Python 组件仍被回收后，监督器才报告注入失败。

## 最终定向验证

```sh
.venv/bin/python -B -m pytest -q \
  tests/test_sap_workbench_display_process_cleanup.py \
  tests/test_sap_workbench_display_deployment.py \
  tests/test_sap_workbench_display_upstream.py
```

结果 **52 passed / 1 skipped in 0.42s**。相对于此前普通组合增加 **5 项**：短暂 PermissionError 后 ESRCH、TERM/KILL/wait 三种持续错误的其他组隔离、以及真实另一组仍清理后报告持续 PermissionError。上游联网测试本轮默认跳过，不借此前真实桥接成功声称修复后的容器通过。

独立子解释器重复十次运行 `tests/test_sap_workbench_display_process_cleanup.py`，每次 **6 passed**（0.29–0.51s），合计 **60 项该文件专项通过**，包括十次父已退出的真实 orphan 组等待实际 ESRCH。本代理不再重复全套，由根代理统一执行。

Linux 镜像构建、Chrome/Xvfb/沙箱、VNC 认证/画面/输入与场景绑定仍未验收；`runtime_verified=false`、`workbench_binding_verified=false` 和任务勾选未改。
