# 所选本地目录读取复测（2026-10-01）

本记录对应本 change 第 5 阶段。与 `evidence.md` 第 4 阶段的隔离测试不同，本次使用用户当前运行的 macOS 开发客户端、真实本机后台、Rock / RC001、原会话与原只读目录 `模拟财务报表_2024`。未修改真实账号、角色或工具权限。以下时间为北京时间。

## 根因及修正

| 实机证据 | 缺口 | 修正 |
| --- | --- | --- |
| 20:59 已绑定目录；21:00 模型只调用 bash，没有设备命令 | 已验证引用仅在工具实例中，模型不知道本轮已选输入 | 按轮同步 Agent 与工具引用，提示本地输入来源；清除、换目录及工具不可用有对应处理 |
| 桌面日志持续 `handshake_failed`，无连接 lease；21:08 工具命令排队后离线超时 | 本地 WSGI Web 端口没有 WebSocket 网关 | 后台运行既有 gateway，主进程从已登记本机 metadata 发现动态回环端口 |
| 21:16 首次握手成功后目录标签被清除 | 文件面板 watcher 把连接 epoch 变化当成目录授权变化 | 授权键保留 workspace、binding、选择代际、grant 版本；连接恢复不清除选择 |
| 21:19 list 成功，materialize 返回 `missing_tenant` | 原生文件传输请求漏带租户头 | reserve/chunk/commit 每次从 broker 读取 `X-Tenant-ID`；无租户不发送 |
| 21:22 上传已成功，但工具只返回 transfer 编号，模型随后猜路径 | 上传完成与既有服务器落盘步骤未接通 | 同一次 materialize 调用返回实际分析路径，核对用户、租户、智能体与命令归属 |

修复过程中也纠正了根目录调用示例：`client_files({"op":"list"})` 必须省略路径参数，不能传空字符串或 `.`。中英文示例均通过真实命令契约校验。

没有新增身份体系、权限放行、命令轮询通道或默认租户。远程来源仍使用原同源网关；本次只补实际复现链路的接线与参数。

## 最终实机结果

21:26:33，在原会话发送自然语言请求：

> 请列出当前选中文件夹中的财务报表，并实际读取资产负债表表头，确认公司名称和报告年度。只验证读取，不做分析或修改文件。

- 21:26:37：`client_files(op=list)` 成功，设备命令 `dcmd_phgUBVe9tC2vR8IddW6Eld28` 为 `succeeded`；目录中有 9 份 DOCX 报表。
- 21:26:39–40：`client_files(op=materialize, relative_path=01_资产负债表_2024年度.docx)` 成功，返回本轮用户工作目录下的服务器文件路径。传输 `xfer_d6z9Evdcf34lOJBD-ebhIUso` 已提交。
- 21:26:57：解析返回「资产负债表」「编制单位：常州华辰精密制造有限公司」「2024年12月31日」「会企01表」「单位：元」。客户端最终展示读取成功。
- 独立核对原 DOCX 的 `word/document.xml`，上述表头一致；原文件与服务器副本均为 **39,270 字节**，**SHA-256 相同**。
- 验证完成后仍保留原账号、会话和目录选择。没有修改原报表。分析使用服务器副本及临时解析文件。

本机日志位置：`run.log:764252`（列举）、`764259`（materialize 返回路径）、`764328`（实际表头）。日志会继续追加，以上位置仅对应本次运行。

截图保存于工作区忽略目录 `doc/desktop-selected-source/verified-20261001.png`，展示公司名称、报表日期和当前所选目录。

## 自动化验证

Python **145 passed**（9 条 aiohttp 警告，无失败）：

```sh
.venv/bin/python -m pytest tests/test_desktop_selected_source_prompt.py tests/test_desktop_gateway.py tests/test_desktop_meta.py tests/test_desktop_local_context.py tests/test_desktop_local_prompt_priority.py tests/test_shared_asset_prompt_guidance.py tests/test_desktop_publish.py -q --disable-warnings --tb=short
```

客户端 **110 passed，0 skipped**：

- `test_desktop_device_connection.cjs`、`test_desktop_device_client.cjs`、`test_desktop_local_read.cjs`、`test_desktop_ws_client.cjs`：64 passed。
- `test_desktop_project_watch.cjs`：28 passed。
- `test_desktop_materialize.cjs`：18 passed。

`npm run build:main`、受影响文件 `git diff --check`、`openspec validate fix-desktop-relogin-session-sync --strict` 均通过。

## 适用范围与未完成项

- 此次实测的是仓库构建的 macOS 开发客户端。安装包尚未重新打包发布，远程部署和其他平台未实测；第 4 阶段原有未验收项保持未完成。
- 重载后台后使用已有重新登录入口恢复配对，没有将重启本身当作登录问题修复。
- 当前机器默认 `python3` 仍会被系统终止；报表解析最终使用 unzip/文本提取并成功。这是独立运行环境问题，本次未更改系统 Python。
- 解析中一次向租户范围外临时路径写入被既有隔离规则拒绝，后续在本轮工作目录内完成，未放宽隔离规则。
