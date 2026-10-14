# 阶段 3 证据：记忆失败的真实分类与页面动作（任务 3.3）

日期：2026-10-13（Windows x64 / Python 3.12.13）。前置：根因见 `memory-403-root-cause.md`，锁修复见 `memory-403-fix.md`。

## 1. 问题

控制台记忆页对**任何**失败都显示同一句话与同一个图标：

```
catch → memoryShowState(error.message, t('memory_retry_hint'), true)
memory_retry_hint = '请点击刷新重试；登录失效时请重新登录。'
```

而且前端把服务端的机器码丢掉了（`new Error(data.message)`），所以页面根本无法区分：

- **安全拒绝**（`unsafe_path`）→ 提示"登录失效请重新登录"，而重新登录永远不可能解决它；
- **锁被占用**（Windows 上 `msvcrt.locking` 争用约 10 秒后失败）→ 过去以裸 `OSError` 冒出，被上层折叠进 `unsafe_path` 或 `memory_unavailable`，用户看到的是"目录被替换"；
- **身份真的失效** → 应当引导重新登录。

## 2. 改动（最小，两处后端 + 两处前端）

| 文件 | 改动 |
|---|---|
| `common/safe_fs.py` | 新增 `LockBusyError`（与 `UnsafePathError` **并列**，不继承）；`file_lock` 在 Windows 分支把 `msvcrt.locking` 的 `OSError` 翻译成它——"别人正持锁"与"路径不对"必须可区分 |
| `agent/memory/personal.py` | `scope_transaction` 分别捕获：`UnsafePathError` → `unsafe_path/403`（安全拒绝），`LockBusyError` → **`busy/409`**（可重试） |
| `channel/web/static/js/fork/views/memory.js` | 新增 `MEMORY_HINTS` 映射 + `memoryError()`（保留服务端 `code`）+ `memoryHintFor()`；两处 `throw` 改为携带码；失败提示改为按码选择 |
| `channel/web/static/js/i18n/memory-account.js` | 新增 `memory_hint_refused` / `memory_hint_busy` / `memory_hint_identity`（简中、繁中、英文） |
| `tests/fixtures/console_i18n_snapshot.json` | 按"只增不改"登记上述三个键 × 三种语言 |

映射（`MEMORY_HINTS`）：`unsafe_path` / `invalid_entry` / `forbidden` / `capability_disabled` / `not_owner` / `read_only_category` / `personal_write_endpoint` → **拒绝**（明说不是登录问题）；`busy` / `memory_unavailable` / `scope_unavailable` → **稍后重试**；`no_identity` / `unauthorized` / `not_authorized` / `missing_tenant` → **重新登录**；请求形状类码（未知 scope/agent/action 等）走通用兜底。

## 3. 验证（本机实跑）

### 3.1 分类真的分开了（模拟 Windows 锁争用）

```
code=busy status=409 message=记忆正在被其他操作占用，请稍后重试
cause=LockBusyError
```

`python -m unittest tests.test_memory_retryable_classification` → **5 项全部通过**：

- 持锁 → `busy/409`，`__cause__` 是 `LockBusyError`；
- 路径被替换 → 仍是 `unsafe_path/403`；
- 两个异常类不互相继承（一个码盖两种原因是本缺陷本身）；
- 空闲锁不被误报为 busy（中文/空格根，真实取锁成功）；
- Windows 分支：`msvcrt.locking` 抛 `OSError(36)` → `LockBusyError`。

### 3.2 页面动作契约

`node --test tests/test_memory_error_hints.cjs` → **5 项全部通过**：

- 失败路径确实按码选提示（不是一句话）；
- 服务端 `code` 会存活到前端（`error.code = data.code`）；
- 关键映射正确（拒绝≠身份、占用=可重试）；
- **服务端全部机器码都有答案**（扫描 `memory_console.py` + `personal.py` 的码，未覆盖即失败——这条第一次跑就抓出我漏掉的 12 个码，已补齐）；
- 三种语言里 `memory_hint_refused` 都**不含**"重新登录/重新登入/sign in again"，且都含"安全"字样。

### 3.3 无回归

- `scripts/repro/probe-personal-crud-windows.py`：C: 与 D: 盘中文/空格根，四个连续事务，**ALL STEPS OK**；
- `scripts/repro/repro-lock-second-call.py`：`scope_transaction: entered`（锁修复仍有效）。

## 4. 既有问题（不是本次引入，未修改）

`node --test tests/test_console_i18n_parity.cjs` 有 **3 项失败**，与本 change 无关，已核实：

| 差异 | 证据 |
|---|---|
| `coding_unavailable` 三种语言文案与快照不一致 | 词典：`编码服务暂时不可用，请稍后重试。`；快照：`编码服务暂时不可用，稍后会自动重试。`（繁中/英文同样） |
| 快照缺 3 个键 | `account_check_timeout`、`coding_send_in_pane`、`mcp_batch_pending` 只存在于词典 |
| 另一项为"命名空间加载顺序"检查 | 本次未新增命名空间，只改了既有文件 |

本次新增的 3 个键在快照与词典两侧、三种语言各 3 处齐全（已逐键核对）。这些漂移属其他改动的登记欠账，**按"不顺手扩大范围"未修**，留给对应 change。

## 5. 未验证

- 真实浏览器里的记忆页渲染与提示文案（需要登录会话与页面交互）：归阶段 7/11 的真实环境验收；本机验证到"码 → 提示键 → 三语文案"的契约层。
- 真实双进程锁争用（两个进程同时写同一记忆）：本机以注入 `msvcrt.locking` 失败模拟同一代码路径，未做真实并发进程实验。
