# 实施前基线

- 功能来源：master `48c0d79c36146950667f8d1884ab823deae62305`，内容指纹见 master-baseline.json。
- 初始工作区：rdai；已有能力中心菜单更名的 6 个受版本控制文件修改及相应未跟踪 change，本次保留。
- 当前实际装载：chat.html → assets/js/console.js；正式记忆 API 活跃实现在 channel/web/fork/handlers/memory.py，经 memory_console.py 适配。
- 实施前复跑 6 个后端记忆测试文件：133 passed / 1 failed。失败为 test_shared_knowledge_is_indexed_without_crashing；该用例使用 knowledge/log.md，而当前同步显式排除 index.md/log.md 控制记录，后续需按知识有效文件契约核对。
- 三个已复现缺陷：个人请求被自动追加 agent_id、正式个人读为只读且不支持记录类别、memory_add 仅写索引未落盘。
- 单写进程前置不能靠部署假设保证，本次补充锚定文件锁，覆盖个人服务与同步发布路径，再以并发用例验收。
