## Context

智能体管理详情面由 `channel/web/static/js/console.js` 的一份手写模板渲染，没有组件层：`renderAgentDetail()` 用模板字符串一次性铺出「概况」页签的全部 `.agent-field`，`renderAgentCapabilitiesPane()` 同样一次性铺出「能力」页签的三个 `.agent-cap-section`。字段的读写因此是**按 DOM 元素标识约定**耦合的：保存路径靠 `document.getElementById(...)` 取控件当前值。

这一结构决定了本变更的关键约束：撤掉控件会改变保存路径读到的值。所幸既有 `saveAgentProfile()` 已经写成「取不到元素就回退到内存里的智能体对象」：

```startLine:4112:4126:channel/web/static/js/console.js
function saveAgentProfile() {
    const agent = findAgent(selectedAdminAgentId);
    if (!agent) return;
    const catEl = document.getElementById('agent-edit-category');
    const sceneEl = document.getElementById('agent-edit-scene');
    const payload = {
        name: document.getElementById('agent-edit-name')?.value.trim(),
        description: document.getElementById('agent-edit-description')?.value.trim() || '',
        position: document.getElementById('agent-edit-position')?.value.trim() || '',
        category: catEl ? (getDropdownValue(catEl) || '') : agent.category || '',
        tags: [...new Set((document.getElementById('agent-edit-tags')?.value || '').split(/[,，]/).map(s => s.trim()).filter(Boolean))],
        greeting: document.getElementById('agent-edit-greeting')?.value.trim() || '',
        persona_summary: document.getElementById('agent-edit-persona')?.value.trim() || '',
        scene_id: sceneEl ? (getDropdownValue(sceneEl) || '') : agent.scene_id || '',
    };
```

同理，两个下拉的初始化函数在元素缺失时本就 early-return（`if (!dd) return;`），SOP 区块的事件绑定全部用 `?.` 与 `querySelectorAll` 挂在已渲染的节点集合上。

## Goals / Non-Goals

**Goals:**

- 三个控件从渲染层消失，且**不触碰保存路径的逻辑**——靠既有兜底让这三个字段原样回传。
- 保持 `category` / `scene_id` / `sops` 的服务端契约、持久化与运行时语义完全不变。
- 让「隐藏」这件事有一条可回归的断言，而不是靠肉眼确认截图。

**Non-Goals:**

- 不删除任何 i18n key。键位在 `static/js/i18n/agents.js` 里是三语与快照测试的载体，删 key 会连带改动 `tests/fixtures/console_i18n_snapshot.json` 与 parity 用例，收益为零。
- 不动服务端（`agent/admin.py`、`channel/web/fork/handlers/agents.py`）与 `agent/registry.py` 的字段定义。
- 不动 `desktop/dist/renderer/js/console.js`（构建产物，与 Web 源本就不同源）。
- 不为这次收缩引入 feature flag：撤下的是三处无管理闭环的控件，没有按租户回退的诉求；保留字段本身已经是回退手段。

## Decisions

**1. 删渲染，不删保存逻辑。**
备选是「保留 DOM 但用 `hidden` 类包起来」。不选的理由：`hidden` 的控件仍会被 `document.getElementById` 取到，保存路径继续读它的值，等于埋了一个不可见的配置入口；而且读者会误以为它随时会回来。直接删块则让「界面不提供入口」与代码事实一致，且既有的 `catEl ? ... : agent.category || ''` 兜底自动接管。

**2. 连带撤除只服务这两个控件的辅助函数。**
`refreshAgentCategoryDropdown()` / `refreshAgentSceneDropdown()` / `sceneCategoryOptions()` 在控件删除后没有任何调用者。保留即死代码。`sceneCatalog()` 不撤——`refreshAgentSceneDropdown` 之外它没有被别处引用，但它与 `sceneCatalogOptions()` 是同族 helper，一并撤除；`_sceneCatalogCache` 随之删除。注意 `tests/test_tenant_default_agent_frontend.cjs` 与 `tests/test_user_default_agent_frontend.cjs` 在它们的测试上下文里 stub 了这两个函数，撤除后 stub 变成无害的多余声明，不必改。

**3. SOP 区块：只删 UI，事件绑定一并删。**
`renderAgentCapabilitiesPane()` 的 SOP 部分包含三处：区块标记、`#agent-sop-add` 的 click 绑定、`.agent-tag-x[data-sop]` 的移除绑定。三处全部删除，避免留下永远匹配不到节点的监听；只为该区块服务的局部量 `const sops = agent.sops || []` 一并不再读取。`sops` 字段本身仍由 `agent` 对象承载，只是页面不再有产生它的控件。

**4. 保存路径不改：宁可回传现值，也不清空。**
`category: catEl ? ... : agent.category || ''` 在控件消失后恒等于 `agent.category || ''`。对已有非空 `category` 的智能体，这是一次同值写入；对空值智能体，仍是空值。这与 spec 里「保存后不回退」的场景一致。备选是干脆不发送这两个键，但那会让「保存时字段被服务端按缺省处理」的风险重新出现——`agent/admin.py` 用 `_UNSET` 哨兵区分「未提交」与「提交空值」，不发送才是不安全的那个方向。

## Risks / Trade-offs

- **既有绑定场景的智能体再也无法从控制台改绑** → 这是本变更的既定代价，spec 已写明字段仍可经 API 读写。缓解：`agent/admin.py` 的 `update_agent(scene_id=...)` 与 `_scene_exists` 校验保持可用；克隆路径（`source_profile.scene_id`）不受影响。
- **`sops` 的界面入口消失后，WorkBuddy 预设写入的 SOP 在控制台上「只读不可见」** → 缓解：预设导入（`scripts/build_workbuddy_presets.py`）与运行时注入（`bridge/agent_bridge.py` 的 `## 📋 SOP` 段、`agent/effective_capabilities.py` 的场景继承）都不经过界面，功能不受影响。
- **误删导致保存路径回传空值** → 缓解：为「已绑定字段在控件缺失时仍原样回传」写一条断言，锁住兜底行为（tasks 3.2）。
- **测试基座改动风险**：`tests/test_agent_profile_frontend.cjs` 目前直接给 `agent-edit-category` / `agent-edit-scene` 的桩节点赋 `_ddValue` 并断言 payload。撤除渲染后这条断言不再描述现实，必须改成断言兜底路径，否则测试会变成「通过但无意义」。

## Migration Plan

无数据迁移。上线即生效，回退即把三处渲染块与 helper 还原（`git revert` 该提交），期间服务端与数据层未被触碰，不存在需要前滚修复的状态。

## Open Questions

（无。）
