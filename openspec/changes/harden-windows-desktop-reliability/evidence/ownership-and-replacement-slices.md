# 阶段 3 证据：归属与防替换切片全部通过（任务 3.4）

日期：2026-10-14 00:2x（本机 Windows x64，**开发者模式已开启**：`HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock\AllowDevelopmentWithoutDevLicense = 1`）。

任务原文：*验证现有真实归属和防替换切片：junction、symlink、中间目录及实际操作期间替换、跨用户/租户访问均不可越界；未满足安全边界不发布对应修复。*

## 1. 为什么此前只能记为"环境受限"

本会话早前没有 `SeCreateSymbolicLinkPrivilege`，任何需要真造符号链接的用例都抛 `WinError 1314`/`EPERM`，因此 symlink 切片无法验证（junction 切片已由 `is_link_like` 修复并验证）。用户开启开发者模式后，同一批套件在同一天内在**同一台机器**上重跑，全部通过。

## 2. 五类切片 × 用例 × 结果（前后对照）

| 切片类别 | 用例 | 开启开发者模式前 | 现在 |
|---|---|---|---|
| **junction** | `tests/test_junction_refusal.py`（6） | 6 passed | **6 passed** |
| **symlink** | `SymlinkRefusalTests`（5：条目/中间目录/根被拒、unlink 拒删链接、写入拒绝跟随链接）、`ReadTests::test_stat_reports_a_symlink_as_a_symlink`、`LockFileTests::test_a_symlinked_lock_path_is_refused_as_shipped`、`…_on_the_fallback_path` | 其中 9 项失败（`WinError 1314`） | **全部通过** |
| **中间目录替换** | `SymlinkRefusalTests::test_symlinked_intermediate_directory_is_refused`、`ReplacementRaceTests::test_root_ancestor_link_cannot_redirect_owner_access`、`…::test_swapping_the_parent_to_a_link_does_not_read_the_target` | 失败 | **全部通过** |
| **操作期间替换** | `ReplacementRaceTests`（2）、`ConcurrentCreateTests`（3：对端创建的层级、间隙里被放成文件、间隙里被放成链接） | 失败 | **全部通过** |
| **跨用户/租户不可越界** | `PathGrammarTests::test_traversal_cannot_reach_outside`、`ReadTests::test_lists_names_without_following_links`、`tests/test_memory_console_scope.py`（33）、`tests/test_personal_memory_scope_protocol.py`（17）、`tests/test_tenancy_isolation_acceptance.py`（6）、`tests/test_memory_storage_tenant_scope.py`（5） | 后四套此前已通过 | **全部通过** |

## 3. 套件级前后对照（同一台机器、同一天）

| 套件 | 之前 | 现在 |
|---|---|---|
| `tests/test_safe_fs.py` | 9 failed / 15 passed / 3 skipped | **26 passed / 1 skipped / 8 subtests passed** |
| `tests/test_memory_index_rel_path.py` | 5 passed / 1 failed（`test_a_root_reached_through_a_symlink_still_matches`，`WinError 1314`） | **6 passed** |
| `tests/test_junction_refusal.py` | 6 passed | 6 passed |
| `tests/test_memory_console_scope.py` | — | **33 passed** |
| `tests/test_personal_memory_scope_protocol.py` | — | **17 passed** |
| `tests/test_tenancy_isolation_acceptance.py` | 6 passed | 6 passed |
| `tests/test_memory_storage_tenant_scope.py` | 5 passed | 5 passed |
| `tests/test_desktop_isolated_preview.cjs` | 26 passed / 1 failed（`EPERM: symlink`） | **27 / 0** |
| `tests/test_desktop_project_native_actions.cjs` | 38 / 2 | **40 / 0** |
| `tests/test_desktop_device_artifacts.cjs` | 14 / 2 | **16 / 0** |

## 4. 判定

**3.4 勾选**：五类切片在本平台真实可造链接的条件下全部通过，没有任何越界被放过；此前记录的唯一失败原因是**本会话缺少符号链接权限**（环境），不是安全边界不成立。修复（`is_link_like` 覆盖 junction 与 symlink、写入路径补根断言）未被回退，仍由上述用例守住。

**环境要求（记录）**：真机验证符号链接切片需要**开发者模式或管理员权限**；`test_safe_fs.py` 仍有 **1 项跳过**（该用例自述的跳过条件，非失败）。
