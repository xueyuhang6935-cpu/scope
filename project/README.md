# Agent 核心目录

| 目录 | 职责 | 当前状态 |
| --- | --- | --- |
| agents | 角色与判断 | M2 抽取报告协议，调查循环暂留后端 |
| tools | 独立外部能力 | M3：SQL 协议、校验与结果处理、调用保护；连接和审计存储由后端注入 |
| skills | 任务方法 | 仅保留职责说明 |
| workflows | 顺序、分支与回退 | M5 才迁移 |
| prompts | 行为与输出规则 | M1：模板与加载器 |
| context | 当前任务信息 | M2：已授权数据的纯组装 |
| memory | 长期有用的信息 | 暂无长期记忆实现 |
| tests | 程序检查 | 本次迁移专项；旧测试位置不变 |
| evals | 任务质量评估 | M6 才迁移，当前仍用 backend.eval_supply_agent |

实际迁移状态以 `docs/agent-migration/status.md` 为准。本目录不读取 `.env` 或直接导入 Web 应用；业务权限、数据库和源快照筛选由后端负责。
