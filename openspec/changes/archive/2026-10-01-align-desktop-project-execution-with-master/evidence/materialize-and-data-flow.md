# 显式上传/分享与数据流说明（任务 9.7，验收 A25 的数据流部分）

对应主规范 `desktop-project-artifacts` 的两条 requirement：

> 设备离线、授权失效或从另一设备访问历史时，系统 SHALL 保留可见的获权产出元数据并
> 解释文件当前不可访问。**只有用户明确选择上传、分享或传输且通过现有权限/额度检查后，
> 系统 SHALL 建立服务器副本或目标副本；文件内容不因卡片展示被默认上传。**

> 系统 SHALL 说明文件可保存在本机，但工具输出、摘录、错误和必要产出元数据会返回服务
> 器并可能进入模型上下文；MUST NOT 因采用本机执行而宣称所有数据不出设备。

## 一、本轮缺的是什么

`transfers` / `publish` / `client_files` 这套服务器能力在 `add-desktop-remote-web-workbench`
里就建好了，而且有配额预约、归属校验、完整性闸门与审计。缺的是**设备那一半**：

```
client_files(op='materialize', relative_path=...)   ← 服务器排队一个 materialize 命令
        ↓ 设备租约
device-ops.runDeviceCommand('materialize')          ← 这里以前一律 feature_unavailable
        ↓ ？
transfers.create → chunks → commit                  ← 走不到
        ↓
artifact_ref（desktop-inputs/<transfer>/<name>）     ← 所以从来没有产生过
```

`desktop/src/main/remote/device-ops.ts` 在 `fix-desktop-local-context-and-tool-calls` 里
明确写过：*`materialize` 与 `inspect` 回 `feature_unavailable`，而不是伪造成功*。那句话
当时是对的，但它同时意味着**用户没有任何办法把一份本机文件交给会话**——「不自动上传」
成立，「明确上传」不存在。本轮把第二半做出来。

## 二、交付内容

### 2.1 编排只有一处：`local-files/materialize.ts`

一次显式交付要走完五步，任何一步省略都会变成另一件事：

| 步骤 | 检查 | 省掉会怎样 |
|---|---|---|
| `stat` 目标 | 必须是普通文件（`not_a_file`） | 目录被当成"没有字节的文件"上传，一次失败变成一次空交付 |
| 大小 | ≤ `file_max_bytes`（`limit_exceeded`） | 字节白跑一趟，最后由服务器拒绝 |
| 版本 | 调用方批准过就**必须**相等（`file_changed`） | 用户点头之后文件又被改过，发上去的是**另一份**文件 |
| 上传 | 复用 `uploadWithSingleChunkWindow`（单窗口、一次 `file_changed` 重试、最终整文件摘要） | 第二次实现分块、重试与摘要 |
| 提交 | 必须有 `artifact_ref`，否则 `publish_failed` | 模型拿着一份**并不存在**的服务器副本继续往下走 |

字节**不经过新的文件描述符**：读取走 fs-guard（`guardSourceHandle`），于是一条既有性质
自动继承下来——**本模块里没有任何地方能拼出绝对路径**，而 helper 每次读取都重新核对
root 描述符，所以撤权会在上传中途生效，而不是等到下一条命令。

窗口是 4 MiB（契约的 `transfer_chunk_max_bytes`），helper 单次只肯给 32 KiB
（`native/fs-guard/src/ops.rs::MAX_READ_CHUNK`），所以窗口由**有界的多次读取**拼出来。
这个常量不是估的：`MAX_READ_BYTES` 是 1 MiB，一次要 4 MiB 会**被 helper 拒绝**（不是
被夹回），因此"按 helper 的上限去要，在自己这边拼窗"是可观测的行为，有专门用例。

### 2.2 来源关联：`source_ref` / `source_version`

服务器副本不是孤立的字节，它带着"这份文件是从哪来的"：

* `source_ref = desktop-file:<workspace_id>:<relative_path>` —— 结构上**装不下绝对路径**
  （有断言：`/Users/...` 不出现在里面）；workspace 是服务器**自己命名**的那个绑定；
* `source_version = "<size>:<modified>"` —— 与设备 `stat` 结果**同一套词表**，调用方
  批准的就是它，传输创建与提交用的是同一个字符串（服务器只把它当不透明值做前后比对）。

传输以 `(command_id, source_version)` 幂等（`create_transfer` 的既有语义），而
`command_id` 来自命令帧的 `request_id`——服务器自己排队的那个命令，所以重放不会变成第二份副本。

**本地引用不被覆盖**：这条路径只读不写。`materializeLocalFile` 结束后本地文件字节与目录
内容原样（有用例逐字节比对），前端卡片仍是本机引用（9.6 已钉住：本机卡片没有
`raw_url`/`preview_url`/`abs_path`），服务器副本是**新增的另一个引用**。

### 2.3 路由与传输：拒绝要留自己的码

* `device-ops.ts` 的 `materialize` 分支：没有相对路径 → `invalid_request`（调用方写错了）；
  **这个 build 没有上传口** → `feature_unavailable`，并说"cannot deliver a local file to
  the server"——"这台机器做不到"与"这台机器刚才坏了"是两件事；
* `materialize-transport.ts` 的三个调用就是既有的三个端点（`/api/desktop/transfers`、
  `/chunks/<offset>`、`/commit`），**没有第二套传输实现**；origin 在这里校验（https，
  只有注册过的 loopback 才允许 http，不许 userinfo/path/query），没有会话时直接
  `auth_required` 而不是发一个未认证请求；服务器的错误码原样保留（`file_changed`、
  `chunk_conflict`、`limit_exceeded` 都是用户能据此行动的信息，压成通用失败就是把它扔掉）。

### 2.4 数据流说明（规范第二条 requirement）

以前**没有任何面向用户的表述**，因此也就没有"错误的表述"；本轮补上，并且把它放在用户
真正会问这个问题的地方——**本机项目的面包屑**（`workspace.js::renderWorkspaceBreadcrumb`）：

```
文件保存在本机；工具的输出、摘录、错误以及引用产出所需的元数据会返回服务器并可能进入模型
上下文。整份文件只有在你明确要求上传或分享时才会传上去。
```

三份词典（zh / zh-Hant / en）都有；服务器路径**不带**这句话（那里的文件本来就在服务器上，
贴上去是噪音）——两种情形各有用例，且断言了那句绝不能被写的谎（`不出设备` / `never leaves`）。

## 三、验证

| 层 | 用例 | 结果 |
|---|---|---|
| 设备端编排（真 fs-guard 子进程 + 真磁盘文件 + 记账式传输桩） | `tests/test_desktop_materialize.cjs` 17 项 | 17 通过 |
| 设备端路由 | `tests/test_desktop_local_read.cjs`（新增 4 项 materialize） | 24 通过 |
| 页面（面包屑 + 三份词典） | `tests/test_desktop_project_native_actions.cjs`（新增 2 项） | 36 通过 |
| 传输窗口与契约常量一致 | 同上：`transfer_chunk_max_bytes` / `file_max_bytes` 取自 `contracts/desktop/v1.json` | 通过 |
| 回归 | `node --test tests/test_desktop_*.cjs` 737 通过；`pytest tests/test_desktop_*.py tests/test_mutation_evidence_scripts.py` 1233 通过 / 807 subtests | 全绿 |

**变异检查**：`evidence/scripts/mutate_materialize.py` 五层（编排 / 窗口与分块 / 路由 /
传输 / 页面与词典）**16 类走样 16/16 命中**（日志 `evidence/materialize-mutations.log`），
已纳入 `tests/test_mutation_evidence_scripts.py` 结构守卫（锚点唯一、只改声明过的源码、
每条都有预期失败）。

## 四、坑（写下来而不是记住）

1. **"变异没被发现"可以是变异不可观测，而不是用例太弱。** M6（一次读满整窗）最初报
   "未被发现"：在 40 KiB 的用例里，helper 会把请求**静默夹回** 32 KiB，循环随后照样把窗
   拼齐 —— 这次改动在**那个区间里是等价的**。换成 1.2 MiB（越过 helper 自己 1 MiB 的
   请求上限，helper 直接拒绝）之后它才第一次可观测。期望写在哪个用例上，和实现改坏没有，
   是两件事。
2. **端口的形状是被数据流决定的，不是被省事决定的。** `ChunkPoster` 原来只收
   `(offset, body, sha256)`，传输实现在 `create` 时拿到 id 之后只能把它记在模块级变量里
   —— 两块上传一并发就会把 A 的字节追加到 B 上。改成每个分块都带自己的 `transfer_id`，
   并且 `TransferCreate` 带上 `command_id`（传输的授权来源就是那个命令）。

## 五、边界与未做

* **用户入口是对话，不是一个按钮。** 明确交付的触发点是用户在会话里要求把某份本机文件
  交给这个会话（`client_files(op='materialize', relative_path=...)`），随后可再用
  `transfer_id` 把它落到运行的服务器工作目录（F16 的离线安全路径，`_materialize_committed`）。
  面板上**没有**每张卡片一个"上传到服务器"的按钮；这是有意留下的：一个卡片动作要连带
  出命令排队、轮询与服务器副本的展示，而"未经要求不上传"这条性质不依赖它成立。
* **补一个明确的限制**：设备端 `expected_version` 的词表是设备 `stat` 的
  `<size>:<modified>`（整秒），不是 9.1 卡片里那个 `sha256:` / `stat:<size>-<ns>` 版本；
  卡片版本用于"这张卡片还指得着吗"，上传版本用于"你批准的是这一版字节吗"。两者目前是
  两套字符串，交接处没有做换算——模型拿到的是 `stat` 的结果，因此这条路径自洽，但
  **从卡片直接发起上传**需要先做这层换算。
* 安装件内真机端到端（真实模型工具消息、签名后的设备）属 10.1–10.3；Windows 未验证。
