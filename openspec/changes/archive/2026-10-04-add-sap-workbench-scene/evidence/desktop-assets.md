# 桌面独立静态资源准备

2026-10-03。本轮仅完成本机文件准备和离线运行环境检查；没有启动或重启服务，没有打开浏览器、创建工作台会话、调用模型、连接 OpenCode/SAP/MCP，也没有同步配置、数据库、登录状态或凭据。用户暂停的实际操作检查继续暂停。

## 缺口与路径

源码 Web 的既有静态构建位于 `/Users/jiantan/ai_assistant/rsmagent/scenes/sap_workbench_assets`。源码桌面使用独立 `COW_DATA_DIR=/Users/jiantan/.cow`，但其 `scenes/sap_workbench_assets` 原先不存在。`runtime_paths` 从当前后端的数据根寻找这些资源，因此仅准备 Web 目录不能满足桌面的本机运行检查。

本轮源目录为上述 Web 构建；目标为 `/Users/jiantan/.cow/scenes/sap_workbench_assets`。没有从当前漂移的 OpenCode 源码重新构建，复用同一份既有 Web 静态产物，不扩大已有兼容结论。

## 复制与一致性

复制前确认源目录绝对路径且 `assets_ready` 通过。检查静态树的文件类型，拒绝符号链接及非普通文件；只处理该资源目录。目标尚不存在，先完整复制到同一父目录中的临时目录，逐文件核对相对路径、长度和 SHA-256，再确认源目录未变化且入口检查通过，最后重命名为正式目标。若目标已存在则只核对，不覆盖。临时目录已发布，无残留 staging 目录。

| 核对项 | 源与目标结果 |
| --- | --- |
| 普通文件数 | 1787，完全一致 |
| 文件总字节数 | 84,559,232，完全一致 |
| 每个相对路径的文件长度及 SHA-256 | 全部一致 |
| `assets_ready` | 两侧均通过 |

把逐文件 `{相对路径: [长度, SHA-256]}` 映射按键排序，以无多余空白的 JSON 序列化后计算 SHA-256，两侧均为：

```text
e5581b5123c2c20aa0b1b38ba0e1c0e9731458490f0879b85869b3e9aa0dada9
```

没有复制 `config.json`、场景 SQLite、OpenCode 历史、runtime、Chrome profile 或其他数据根内容。原 Web 静态目录和 `rsmcode` 源码保持原状。

## 桌面数据根离线检查

沿已有现场证据中的可信项目 `/Users/jiantan/ai_assistant/sapwork` 执行以下命令；没有读取或输出真实配置中的秘密：

```sh
SAP_MCP_PYTHON=/Users/jiantan/ai_assistant/rsmcode/sap-connect/sap-pyrfc/.venv/bin/python \
  .venv/bin/python -B -m Scene.sap_workbench.backend.environment \
  --data-root /Users/jiantan/.cow \
  --project /Users/jiantan/ai_assistant/sapwork \
  --browser-ref sap-browser-worker
```

显式指定的 MCP 解释器由其 `pyvenv.cfg` 离线元数据确认版本为 3.10.0，没有执行它或导入 MCP SDK。命令退出码为 0，输出 `mode: offline_local_environment`、`network_tested: false`；`browser`、`opencode_runtime`、`embed`、`project`、`mcp_runtime` 五项均为本机 `passed`。

环境命令只核对执行文件/目录访问权限和 HTML 入口引用；不会创建项目、数据库或运行目录。文件完整复制不保证软件版本兼容、Chrome 能启动、平台登录成功、模型可调用或 SAP/MCP 登录成功。主后端和桌面原进程本轮未重启，新场景补丁仍待后续正常重启加载。这项准备不代替任务 7.2 的桌面双栏、任务 5.7 的真实控件及 G0–G4 现场验收，不能把提交或完整阶段标记为完成。
