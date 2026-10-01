# 运维手册 — 桌面远程工作台（草稿）

Change: `add-desktop-remote-web-workbench` · task 17.5

## 同源网关与反向代理

- Web 与设备 WSS 必须同源 HTTPS。
- 内部 gateway 默认仅环回；由反向代理做 Upgrade，不把内部端口暴露公网。
- 示意见 `evidence/phase-2-group-9.md`。

## 本机目录读取依赖（`fix-desktop-local-context-and-tool-calls`）

本机目录的读取**只能**经设备通道完成，部署侧要同时满足两点：

1. `/api/desktop/connect` 在该源上可达（同源反向代理把 Upgrade 转到 `python -m integrations.desktop`）。
   客户端从配置源推导 WS 地址，不读页面给的 URL；非环回源仍要求 `wss://` 且证书可验证。
2. 客户端在**绑定确认后**才开始设备连接（选择目录之前不会有连接）。用 `GET /api/desktop/meta`
   确认 `desktop_local_files` 可用、`GET /healthz`（gateway）确认进程在跑。

不满足时行为是**如实失败**而不是回落：工具返回 `device_offline` / `deadline_exceeded`，
不会改读服务器默认目录。排障时可对照 `stale_context` / `grant_revoked`（授权已撤销或被替换）。

## 容量与保留

| 项 | 默认 |
|---|---|
| 单文件 | 512 MiB |
| 传输超时 | 30 分钟 |
| 未提交暂存 TTL | 24 小时 |
| 已发布任务输入 | 任务结束后 7 天（运行引用延长） |

存储配额使用 `quota_limits.storage_bytes` 存量预留，不随日窗口清零。

## 签名 / feed

生产签名、公证与更新 feed **未在本切片宣称完成**（构建延期）。未配置时不得开启生产客户端发布。

## 诊断

客户端导出诊断含版本、协议、连接错误码与脱敏关联 ID；不含 token/Cookie/绝对路径/正文。

## 逐平台开关

| 开关 | 前置证据 |
|---|---|
| `desktop_remote_web_enabled` | 阶段一 |
| `desktop_local_files_enabled` | 阶段一 + 阶段二（含平台 helper） |
| `desktop_native_notifications_enabled` | 阶段三 A + 真实通知源 |
| `desktop_local_processing_enabled` | 阶段二 + 平台运行器约束 |

缺证据时保持关闭。

## 回退

1. 关解析与文件开关  
2. 停止新任务、取消未提交传输  
3. 排空网关  
4. 撤销桌面父子会话与租约  
5. 回退旧服务端前必须先撤销配对  

备份恢复不得复活授权。详见 design Migration Plan。
