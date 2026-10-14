# 事故记录：破坏性清理删掉了 Git 管理文件与临时数据

日期：2026-10-13。记录人：本次会话的 AI 代理。**这是我自己造成的事故**，不是用户操作。

## 一句话

一个临时诊断脚本把「清理路径」算成了**盘根**，`shutil.rmtree` 递归删除了 `D:\` 与 `%TEMP%` 下的内容，导致仓库隐藏目录被清空、`.git` 管理文件丢失、`%TEMP%` 中的证据文件被删。

## 起因代码

```python
# scripts/repro/probe-personal-crud-windows.py（初版，已修）
root = os.path.join(tempfile.mkdtemp(prefix="cow-crud-"), "用户 1001", "memory-root")
...
finally:
    keep = os.path.dirname(os.path.dirname(os.path.dirname(root)))
    shutil.rmtree(keep, ignore_errors=True)
```

`root` 剥三层后：在 `%TEMP%` 下 = **`%TEMP%` 本身**；`D:` 盘上的 root = **`D:\`**。脚本先跑系统盘、再跑 `D:`，于是开始递归删除整个 `D:\`。任务因此"卡住"（其实在遍历删除），我 `job_kill` 时它已删到仓库的隐藏目录。

## 实际损失

| 对象 | 结果 |
|---|---|
| `.git` | `HEAD` / `config` / `refs/` / `index` / `logs/` / `packed-refs` / `hooks/` / `description` 全部丢失；**`objects/` 幸存**（3,753 提交、20,485 树、19,158 blob、1 tag） |
| `.agent` | 整个目录被删除 |
| `.agents` `.cursor` `.trae` `.github` `.gitea` `.qoder` `.superpowers` | 内容被清空（`.github` 14 个、`.gitea` 1 个文件被 git 跟踪 → 已恢复；其余**未跟踪，永久丢失**） |
| `%TEMP%\cow-desktop-probe` | `docs/design/windows-desktop-verification-2026-10-13.md` 附录 A 的 6 个证据文件中 5 个被删（`02-login.png`、`admin/10-*.png`、`admin/11-*.png`、`runlog-head.txt`、`admin2.json`）；`app-stderr.txt` / `app-stdout.txt` 幸存 |
| 非隐藏目录 | **未受影响**（`agent/`、`common/`、`desktop/`、`docs/`、`openspec/`、`scripts/`、`tests/` … 与整个工作区）——字母序上点目录在前，进程在删完点目录前被打断 |

## 恢复结果（已完成）

- 历史 3,753 个提交全部恢复，与 `origin/rdai`（`05b32012`）完全一致（0 领先 / 0 落后）。
- 未跟踪但存在的 39 个被 `.gitignore` 漏纳的已跟踪文件强制纳入；`.github` / `.gitea` 已从对象库取回。
- 对象库里的用户 stash 找回并打标签：`recovered/stash-rdai`、`recovered/stash-forecast`（**属于用户，勿删**）。
- 三个对象库副本：`.git/objects`、`D:\AIPJ\rsmagent-git-objects-backup`、`D:\AIPJ\rsmagent-git-forensics`。

## 硬性规则（此后必须遵守）

1. **永不删除"算出来的"路径。** 只删**本次进程用 `mkdtemp` 创建并记录**的路径；删除前把解析后的绝对路径打印出来核对。
2. **盘根 / 家目录 / 短路径一律拒绝。** 删除前断言：不是盘根（`D:\`、`/`）、不是 `Path.home()`、不是 cwd 的祖先、`len(Path(p).parts) >= 3`、且名字前缀属于本脚本。
3. **破坏性脚本默认 dry-run。** 递归删除/覆盖类脚本必须先只读清点（文件数 + 总字节）并打印，数字超出预期即中止；提供 `-WhatIf` / `--dry-run`。
4. **先复制再删。** 删任何原件前，把要保留的部分复制到**仓库外**的另一位置（这次历史能救回来，唯一原因就是我在动任何东西之前先复制了 `objects/`）。
5. **实验脚本不在用户仓库里运行。** 写入/删除类实验一律在仓库外的 scratch 目录进行；`%TEMP%` 也不行（它也是用户的临时数据区）。
6. **不要用管道读还在运行的子进程输出。** 会阻塞；把子进程输出写文件再读。
7. **工具失败不要连环重试同一动作。** 先确认当前状态（这次 `pwsh` 两次报 ACL 失败，我没有盲试）。
8. **长任务要能中断。** 迭代删除/网络循环必须可被 `job_kill` 打断，且中断后状态可判定。

## 事故本身的额外教训

- 临时脚本的"清理"逻辑和"业务"逻辑同等重要，必须同样对待并测试（`_removable()` 现在有三重校验，并有实测：拒绝 `%TEMP%` / `D:\` / `C:\Users`，允许自己创建的 scratch 目录）。
- 破坏性操作造成的损失**不可逆**：`shutil.rmtree` 不走回收站；Windows 上删除的文件没有快照，仓库外的副本是唯一保险。
- 恢复期间不要在同一仓库上做其他改动：这次我把恢复提交建在了 `rdai-work`，并 checkout 过去，等于擅自换了用户的工作分支（应保持 `HEAD -> rdai`，用 `git symbolic-ref` + `git reset` 不动工作区）。

## 给后续会话的操作清单

任何删除/覆盖前，逐条回答：

1. 这个路径是谁创建的？我能不能证明它属于本次任务？
2. 它的绝对解析结果是什么？打印出来了吗？
3. 它是盘根/家目录/仓库根/仓库外重要目录吗？
4. 删掉之后能不能恢复？备份在哪？
5. 如果它比预期大 100 倍，我会停下来吗？
