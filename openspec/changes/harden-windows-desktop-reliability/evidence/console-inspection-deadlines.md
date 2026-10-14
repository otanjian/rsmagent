# 阶段 5 证据：巡检截图的操作期限与失败记录（任务 5.3）

日期：2026-10-13。对应报告遗留问题：`docs/design/windows-desktop-verification-2026-10-13.md` 第 291 行（第 5 条）"CDP `Page.captureScreenshot` 在本次巡检中两次无响应（>60s 不返回），需调用方自备超时"。

## 1. 问题

上次巡检用的是临时脚本：**没有期限**，一次截图无响应就要人工杀掉进程，而且没有记录"是哪一页挂的"、后面的页面也再没被巡检。任务要求正是：操作期限 + 失败记录 + 挂起时有界返回且其它页面继续 + **未证实的产品问题不得标成产品修复**。

## 2. 交付物：`scripts/inspect-console-cdp.py`

| 能力 | 实现 |
|---|---|
| 每个操作都有期限 | `--deadline`（默认 20s）；**双重约束**：会话内部对每个 CDP 调用做 `wait_for`，巡检循环再对每个 `await` 做一次——传输层若忽略自己的超时或卡在更下层，整轮仍然有界 |
| 挂起不影响其它页面 | 每页**独立开/关一个会话**，卡死的连接不会带到下一页；失败页只记自己的账 |
| 失败记录 | 每页一条：`page/status(ok\|timeout\|error)/verdict/elapsed_ms/detail/screenshot/expectation/needs_confirmation`，另有 `summary` 与 `inspection.json` 落盘 |
| 有界等待页面就绪 | 用固定 `--settle`（默认 1.5s）而不是"等网络空闲"——后者正是本次要消除的无界等待 |
| 不越权下结论 | `verdict` 只有 `observed`（页面确实渲染出什么）与 `inspection_limitation`（巡检没能判定）；超时/异常一律是后者，`needs_confirmation=False`；报告 `notes` 明写"仅为观测，缺失的期望需先确认才能称为产品缺陷"；脚本**不写任何"修复"结论** |
| 退出码 | 至少巡检到一页 → 0；一页都没成功 → 2（说明是巡检坏了，不是产品坏了） |

用法见脚本 docstring：

```powershell
python scripts/inspect-console-cdp.py --endpoint http://127.0.0.1:9222 `
    --out .cow-inspect --deadline 20 --pages / --pages /admin --pages /settings/models `
    --expect "/admin=控制台"
```

依赖：`requirements.txt` 里的 `aiohttp`（`--help` 与失败记录路径不需要它）。

## 3. 验证

`tests/test_console_cdp_inspection.py`（**9 passed**，用假浏览器驱动真实循环，不需要 Chrome）：

1. **挂起只花自己那一页的期限**：`/a` 正常、`/b` 截图永不返回、`/c` 正常 → 三页都有记录，`/b` 为 `timeout/inspection_limitation`，`/c` 仍 `ok`；并断言整轮耗时 < 3×期限 + 余量（**有界返回**）；
2. 每页独立会话：三页 → 开 3 次、关 3 次（卡死连接不外溢）；
3. 超时**绝不**被写成产品结论：`verdict != observed`、`needs_confirmation=False`、三者文本中无 "fix"；
4. 非超时异常同样记录为巡检限制且继续巡检；
5. 期望未满足 = **需确认的观测**（`expectation=missing`、`needs_confirmation=True`），不是"缺陷已确认"；
6. 期望满足 = 无需确认；
7. 落盘报告与运行一致：`pages`/`summary`/`notes` 齐全，有应答的那页留下 PNG，挂起那页不产生空文件；
8. 会话自身对有期限约束：用"永不回答的 socket"验证 `CdpSession.call` 抛 `TimeoutError`；
9. 默认页面集合是登记的控制台路径。

端到端演示（真实 CLI，指向死端口）：

```
{"origin": "http://127.0.0.1:9899", "pages": 3, "observed": 0, "inspection_limitation": 3, "screenshots": 0}
  error    inspection_limitation /a — URLError: [WinError 10061] 目标计算机积极拒绝
  error    inspection_limitation /b — …
  error    inspection_limitation /c — …
  退出码=2
```

即：三页各自失败、各自留证、逐页继续，报告落盘，退出码表明"什么都没巡检到"。

## 4. 判定与未验证

- **5.3 勾选**：期限、失败记录、挂起有界返回并继续、可重复脚本、未证实问题不标成修复，均已实现并有测试守住。
- **未验证**：**真实** Chrome/CDP 会话下拍摄真实截图与真实挂起场景（本机没有带远程调试的浏览器会话；验证覆盖到"循环 + 会话期限 + 报告"三层，其中会话期限用真实永不响应的 socket 验证）。真实巡检时如遇 `timeout` 记录，请按报告 `notes` 的说明先确认再定性。
