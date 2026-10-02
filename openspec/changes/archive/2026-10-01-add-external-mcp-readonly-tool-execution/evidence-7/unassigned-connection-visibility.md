# 未分配连接的可见性：为什么红框那条连接被这个智能体看到了

对应提问（2026-09-27 19:23）：`OneAgent HTTP MCP` 并没有分配给「税务健康体检」，为什么该智能体看到了它？

结论：**不是分配被绕过，是这条连接还没进入分配制度。** 它在数据里是
`configured=0`（**未配置 · 沿用原权限**），而提问时的身份是**租户管理员**——
租户管理员豁免是变更前就有的规则，它按「本租户的能力都能用」放行。

复验脚本：`live_unconfigured_connection_visibility.py`，输出
`live_unconfigured_connection_visibility.txt`。只读，不写库、不改配置。

## 1. 「分配表里没有该智能体」有两种完全不同的含义

```
[1] 分配状态行（external_connection_agent_assignment_sets）
    OneAgent HTTP MCP      configured=0 assigned=[]
        assignment_allows(tax-health-check-test15) = allowed=True reason=unconfigured
    weknora-rsmagent       configured=1 assigned=['tax-health-check-test15']
        assignment_allows(tax-health-check-test15) = allowed=True reason=assigned
```

| 状态 | 含义 | 效果 |
| --- | --- | --- |
| `configured=0` | 还没被分配制度接管 | **沿用原权限**（变更前的口径），不因为「没分配」而收窄 |
| `configured=1` | 已配置 | 以关系为准；关系里没写就是拒绝，**租户管理员同样受约束** |

`configured=0` 是本 change 的兼容性设计，写在
`add-external-connection-agent-assignment` 的「按连接首次配置并区分未配置和空分配」：
存量连接初始化为未配置，避免「上线一条连接就把整个租户收窄」。
控制台也已如实标注这一状态（`ec_assign_summary_unconfigured` = 「未配置 · 沿用原权限」，
`ec_assign_unconfigured_note` = 「该连接尚未配置，当前所有智能体沿用原权限。」）。

## 2. 放行来自租户管理员豁免，不是来自分配

```
[2] may_execute() 的三条入口分开看

    --- 租户管理员 ---
        OneAgent HTTP MCP      may_execute=True  grant=False assignment=False
        weknora-rsmagent       may_execute=True  grant=False assignment=True

    --- 普通成员 ---
        OneAgent HTTP MCP      may_execute=False grant=False assignment=False
        weknora-rsmagent       may_execute=True  grant=False assignment=True
```

红框那条连接的 `may_execute=True` 而 `grant=False`、`assignment=False`——两条都被排除，
所以它只可能走第三条入口：`may_execute` 的**租户管理员豁免**（way 2，
`agent_in_scope` + `tenant_admin_may_execute_tool`）。

`git diff HEAD -- integrations/external/authorization.py` 显示：

```
-    Three ways in, and only three:
+    Four ways in, and only four:
...
+    3. **An assigned connection is its own authorization** ...
```

way 2 在 HEAD 中已存在（本次只新增了 way 3）；也就是说「租户管理员对本租户能力都能用」
不是本 change 引入的。本次新增的分配闸门
（`integrations/external/tools.py#_assigned_to_agent`）对 `configured=0` **刻意不收窄**：

```
        if not assignment.call_regime_applies(...):
            out.append(binding); continue
        ...
        allowed, _reason = assignment.assignment_allows(...)
        if allowed:
            out.append(binding)
```

——`configured=0` 时 `assignment_allows` 返回 `(True, "unconfigured")`，因此保留。
两道闸门方向一致：**未配置的连接就是变更前的口径。**

## 3. 最终投影：与现场日志逐字对齐

```
[3] 最终投影（authorized_tools 的 connection-level 名字）
    租户管理员 + 已分配的 agent    (agent=tax-health-check-test15) -> 18
         mcp_resources_read_conn_8r2lE23PRk0Sd_Wb
         mcp_resources_read_conn_cN-lhbMpQlwpiL7k
         mcp_tools_list_conn_8r2lE23PRk0Sd_Wb
         mcp_tools_list_conn_cN-lhbMpQlwpiL7k
         mcp_tools_read_conn_8r2lE23PRk0Sd_Wb
         mcp_tools_read_conn_cN-lhbMpQlwpiL7k
         （+ weknora 的 12 个远端工具，共 18）
    租户管理员 + 未分配的 agent    (agent=knowledge-qa-test15) -> 3
         mcp_resources_read_conn_8r2lE23PRk0Sd_Wb
         mcp_tools_list_conn_8r2lE23PRk0Sd_Wb
         mcp_tools_read_conn_8r2lE23PRk0Sd_Wb
    普通成员   + 已分配的 agent    (agent=tax-health-check-test15) -> 15
         （只有 weknora 的 15 个；红框那 3 个不出现）
```

第一行的 6 个 connection-level 名字，与 `run.log` 19:18:27 那次投放**逐个相同**：

```
19:18:27 [agent_stream.py:1463] [Agent] external tools synced:
  +['external_mcp_resources_read_conn_8r2lE23PRk0Sd_Wb',
    'external_mcp_resources_read_conn_cN-lhbMpQlwpiL7k',
    'external_mcp_tools_list_conn_8r2lE23PRk0Sd_Wb',
    'external_mcp_tools_list_conn_cN-lhbMpQlwpiL7k',
    'external_mcp_tools_read_conn_8r2lE23PRk0Sd_Wb',
    'external_mcp_tools_read_conn_cN-lhbMpQlwpiL7k'] -[]
```

现场身份是租户管理员（界面左下角「租户管理员」，即 `usr_9ZxVPz7M2FuOro1q`，
`memberships.display_name` 同名），不是普通成员。

**第三行是判据**：同一智能体、换成普通成员后，红框那 3 个名字不再出现。
可见性差异来自**提问者身份**，不是来自智能体分配。

**第二行是另一半判据**：同一个租户管理员、换成一个未分配的智能体，
已配置的 `weknora-rsmagent` 的 3 个名字消失，只剩 `configured=0` 的
`OneAgent HTTP MCP`。所以「已配置」的连接对租户管理员一样生效，
分配闸门没有对管理员开后门。

## 4. 结论与操作建议

- **不是缺陷**：`configured=0` + 租户管理员 = 沿用变更前的可见性，两处代码与
  spec 都如此规定，控制台也如实标注了「未配置 · 沿用原权限」。
- **要让它变成「只有被分配的智能体可用」**：在控制台打开该连接的分配弹窗**保存一次**
  （首次保存即原子置为 `configured=1`）。此后：
  - 关系里列出的智能体可用；关系外的智能体**和管理员**都不可见；
  - 保存空分配 = `configured=1` 且无人可用（spec：「已配置但实际分配为空 SHALL 禁止
    所有智能体使用」），**不会**回退到沿用原权限。
- **口径上的一个诚实说明**：「沿用原权限」这个标签描述的是「回到本次 change 之前」，
  而不是「一定看得到」。本次 change 同时补上了 MCP 的 `tools.read` 动作，
  所以这条连接真正可用的动作是这次才打开的；「未配置」只保证分配制度不额外收窄它。
