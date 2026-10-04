# 归档记录

归档日期：2026-10-04。用户在收到最新导航测试失败说明后明确要求归档本 change，按该要求保留当前交付及验证状态。

- Change：`add-sap-workbench-scene`；schema：`spec-driven`。
- 规划产物完整；实施任务完成 49/64，保留 15 项未完成：1.5、1.7、2.9、3.11、4.3、4.4、4.6、4.7、5.3、5.5、5.7、5.8、6.3、6.6、7.2。未为归档修改任务勾选。
- 规格同步范围：新增 `sap-webgui-browser-control`、`sap-workbench-scene`、`sap-workbench-session-binding`；更新 `scene-application-console` 的分类卡片/激活要求和 `scene-workbenches` 的专用分发要求。保留当前原生 iframe 模式限制及后续完整目标的区分。
- 最新对话导航测试未通过：OpenCode 未绑定左侧实际 SAP iframe，不能把模型声称成功或其他浏览器动作视为成功。左侧人工参考导航通过。见 [导航实测](evidence/chat-navigation-test.md)。
- 归档不是完整验收或生产可用证明；统一登录、同页自动控制、提交与其他未验收范围保留为后续工作。
- 之前文档中“不归档”“继续实施”等说明属于当时状态，本次明确归档指令覆盖其后续安排，历史测试结果保留。

最新入口文案及 Chrome 验证见 [文案更新](evidence/scene-copy-update.md)。归档仅整理 OpenSpec 规格、证据和文档引用，不修改应用运行逻辑。
