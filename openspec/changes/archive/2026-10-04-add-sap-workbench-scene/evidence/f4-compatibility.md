# 已观察供应商 F4 搜索弹窗的窄范围兼容

2026-10-04。用户暂缓 SAP Web GUI／OpenCode 的实际操作检查。本轮只修改场景内识别规则、归属检查和隔离测试，未启动 Chrome、调用模型、SAP 或真实 MCP，也未修改真实配置或保存业务。

## 依据与误拒原因

[历史真实事件](current-account-tools.json) seq 101 记录 ME21N 中供应商来源字段 `0:3` 的 F4，seq 111 记录同一控制代次的关闭，seq 121 独立读回无弹窗。seq 101 保存的 `dialog_labels` 为 101 字符、15 个非空行的整段 fallback 文本；没有保存完整 DOM 或原始 dialog `text`。本次仅将这段已观察结构登记为兼容候选，新的快照仍须独立提供完整且匹配的 label **及** text。

旧通用帮助类型只完整匹配“值帮助”“输入帮助”等标题；此窗口标题为“限制值范围 (1)”，并且正文的过滤标签“集中删除标志”触发了通用持久化禁止词。它因此不能建立新的自动 F4 归属。全局删除／保存禁止词未放宽，只有以下完整固定结构例外。

## 支持合同

[effects.py](../../../../../Scene/sap_workbench/browser_service/effects.py) 同时核对 label、text 的全部非空行。只规范行内空白及换行，顺序和内容均须一致；不能只按“限制值范围”前缀、页签角色或任意 dialog 放行：

```text
限制值范围 (1)
搜索并选择
A: 供应商（常规）
执行
加重
隐藏过滤器
搜索词:
国家/地区代码:
邮政编码:
城市:
名称:
供应商:
集中删除标志:
项目 (0)
取消
```

恰好一个 dialog、登记的 ME21N 创建页面、原 F4 派发与稳定回读是建立归属的前提。label 达到快照 240 字符截断边界或 text 达到 2000 字符边界时拒绝，不能以截断内容证明完整结构。新增结果、字段缺失、重排、其他语言／窗口或追加确认文本均拒绝。

自动能力仅为现有 F4 打开、观察和同归属 Escape 关闭。打开后连续两次稳定观察，沿用源业务字段 ID 和控制代次；搜索输入框不成为源字段。[page.py](../../../../../Scene/sap_workbench/browser_service/page.py) 关闭时还要求当前仍是登记 ME21N 页面、同代次及原始 label/text 完全相同。入场时允许的空白规范化不会使已改变的原始字符串继续获得关闭权限。

Escape 之前沿用固定 `VALUE_HELP_READY` 检查唯一弹窗、完整原始 label/text、弹窗内 activeElement 及嵌套 frame 焦点链，并再次核对控制代次。未归属、内容／页面变化、额外模态、失焦或人工接管均不发送 Escape。没有新增原始脚本、坐标、Enter、查询或点击执行的工具。

该结构另有**独立选值禁止规则**：即使页面出现或被注入 `role=option`、`popup=true` 及显式 `value`，也不允许 `choose`。弹窗存在时原 `require_draft` 同样阻止搜索栏填写、嵌套帮助和其他草稿动作；“执行”“搜索并选择”也没有自动按钮／页签入口。

## 不作出的值状态结论

七个过滤标签和“项目 (0)”是观察到的文本结构，**不证明七个输入值为空**。HTML input 的当前 value 通常不进入 innerText；既有 [人工 F4 截图](sap-manual-f4-current.png) 的搜索词已有测试文字，其他文本结构仍可相同。现有 fields 也没有 dialog 归属元数据，所以本补丁不推断、清空或提交过滤值，不声称已支持搜索结果、真实选值、完整分页或页签切换。

全局提交入口仍为 `submission_adapter=None`，保存／过账／删除继续关闭。仅有本次兼容候选及隔离验证不能完成任务 5.3、5.7、6.3 或 G3；实际浏览器/SAP 重测仍暂停。

## 定向验证

```text
.venv/bin/python -B -m pytest -q tests/test_sap_workbench_f4_compatibility.py tests/test_sap_workbench_effects.py tests/test_sap_workbench_controls.py
101 passed in 0.18s
```

其中新增 [F4 专项](../../../../../tests/test_sap_workbench_f4_compatibility.py) 31 项；既有 effects／controls 70 项。历史 seq 101 的脱敏 label 以独立字面量保存在该测试文件，保留原 LF 与 NBSP、注明来源；不从实现白名单反构造，也不在运行时读取活动 OpenSpec 目录，因此归档 change 不会破坏此 fixture。可控页面／CDP 替身验证完整文本、截断边界、错误页／多模态、注入 option 仍拒绝、F4 稳定归属、完整原始 payload、旧代次／失焦／接管／内容变化时零派发，以及搜索栏输入／查询／选择／页签无入口。没有模拟 SAP 业务保存或性能测量。

移除 OpenSpec 活跃路径依赖后，仅重跑该独立专项：**31 passed**。源码识别规则及现场范围未变化，完整套件由主任务统一执行。

```text
node --test --test-reporter=spec --test-name-pattern='^(F4 guard|Escape guard|Enter guard verifies)' tests/test_sap_workbench_dom.cjs
3 pass / 0 fail
```

上述为未修改的固定 DOM guard 中相关焦点／frame／唯一弹窗检查；Node VM DOM 为替身，不运行浏览器。本轮未重复完整套件，既有相关用例与最终主套件重叠，不把数量相加为现场验收数。没有新增服务、浏览器 profile、真实租约或端口需要回收；普通执行器、其他场景及 `rsmcode` 核心未改。
