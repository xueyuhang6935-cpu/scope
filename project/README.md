# Agent 核心目录

M0—M7 已完成。应用入口和宿主服务留在 backend；九目录职责如下。兼容入口、维护及回退规则见 [收尾说明](../docs/agent-migration/compatibility.md)。

| 目录 | 职责 | 当前状态 |
| --- | --- | --- |
| agents | 角色与判断 | M2 报告协议；M4 调查判断循环、计划修订、预算与收尾，模型会话由后端注入 |
| tools | 独立外部能力 | M3 SQL 协议与调用保护；M4 工具分发与已读证据登记；连接和审计存储由后端注入 |
| skills | 任务方法 | 仅保留职责说明 |
| workflows | 顺序、分支与回退 | M5b 图、审批等待、同步检查点和恢复调度；后端注入业务服务与检查点 |
| prompts | 行为与输出规则 | M1：模板与加载器 |
| context | 当前任务信息 | M2 已授权数据摘要；M4 初始报告与模型消息的纯组装 |
| memory | 长期有用的信息 | 暂无长期记忆实现 |
| tests | 程序检查 | M6 核心检查与 integration 宿主集成测试；通用后端及浏览器测试保留原位 |
| evals | 任务质量评估 | M6 固定案例、评分与人工审核；backend.eval_supply_agent 保留宿主 CLI |

实际迁移状态以 `docs/agent-migration/status.md` 为准。运行时核心及评测核心不读取 `.env` 或直接导入 Web 应用；测试可导入后端验证集成。业务权限、数据库和源快照筛选由后端负责。运行命令见 [tests](tests/README.md) 和 [evals](evals/README.md)。
