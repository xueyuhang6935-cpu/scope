# 迁移后的入口与维护边界

M7 核查基于 M6 提交 `99e8e13`。九目录迁移已经完成；目录调整不代表物流 T02—T24 已实现。应用仍由 `scripts/start-backend.mjs` → `python -m backend` → `backend/main.py:create_app` 启动。

## 兼容入口清单

| 保留入口 | 实现与保留原因 | 新代码如何调用 |
| --- | --- | --- |
| `backend.supply_prompts` | 显式转发 `project.prompts.loader` 的加载器、模板和版本；旧导入兼容 | 直接导入 `project.prompts.loader` |
| `backend.supply_context` | 三个上下文函数显式转发 `project.context.investigation`；业务意图与 followup 仍属后端 | 纯摘要调用新路径；业务解析继续调用后端 |
| `backend.investigation_report` | 协议和 validate_report 显式转发 `project.agents.report_protocol`；plan_blockers 保留业务口径 | 协议用新路径；业务服务调用 plan_blockers |
| `backend.investigation_sql` | 受限 PostgreSQL 连接及旧构造函数组装；校验/常量显式来自 `project.tools.investigation_sql` | 纯协议用 tools；实际连接使用后端适配器 |
| `backend.investigation_harness` | 给 `project.tools.executor` 注入 SQLite 审计；interrupt 委托审计存储 | 宿主继续组装，核心只接收能力 |
| `backend.investigation_agent.investigate` | 原签名不变；注入 model_session 和配置可用性，仅向核心传模式及预算 | 业务服务和评测经此组装；独立核心测试直接注入 agents |
| `backend.supply_workflow.SupplyWorkflow` | 业务服务、原路径 SqliteSaver、执行器及兼容节点方法组装；State 来自 workflows | 应用继续从此构造；图逻辑只在 `project.workflows.investigation` 修改 |
| `backend.eval_supply_agent` | CLI、配置与模型适配；样本、评分和审核在 `project.evals.supply_agent` | 命令保持 `python -m backend.eval_supply_agent` |

这些入口没有第二份核心实现，不做通配导出或隐式导入重定向。内部纯核心调用已经使用新路径；带凭证、连接、事务或资源生命周期的宿主入口不是可直接删除的空壳。不发弃用警告，也不在本阶段删除既有导入契约。

若以后要删除纯兼容导出，先检索源码、脚本、文档和外部使用方，明确通知与版本边界，再更新对象同一性检查和使用说明。不能以仓库内没有业务调用为由认定没有外部调用。宿主适配器只有在组装职责有明确替代、调用方切换并完成资源/权限/恢复验证后才能删除。

`backend.test_investigation_*` 与 `backend.test_supply_workflow` 旧测试路径在 M6 已移除，不保留重复发现入口。新位置是 `project/tests/integration/`；原工作流文件拆为 `test_supply_workflow` 与 `test_agent_loop`。案例路径改为 `project/evals/supply_agent_cases.json`，没有旧路径副本。

## 开发与验证

九目录职责见 [project 说明](../../project/README.md)。运行时核心和评测核心不导入 backend/FastAPI、不读取 `.env`；集成测试可导入宿主。权限、模型配置、连接、事务、审计和检查点资源留在后端；skills 与 memory 目前只有职责说明，不会自动变成运行能力。冻结评价答案只用于 evals。

在 Scope 根目录按 [测试说明](../../project/tests/README.md) 运行 backend 与 project 两组发现命令；评测命令及人工审核见 [评测说明](../../project/evals/README.md)。离线默认不开真实模型和 PostgreSQL 集成；跳过项不算通过。隔离数据库场景及角色未就绪时不得重建源库来满足测试。

当前用户已有未提交的 `AGENTS.md` 保持原字节；其中旧目录清单需结合本说明和 README 的当前路径阅读，本阶段不覆盖该用户文件。历史 `plan/` 与物流 T01 基线保留当时事实，不批量替换历史路径。

## 回退与遗留验证

M7 开发回退点为 `99e8e13`。M7 只收尾文档与兼容约定，没有数据库、接口或检查点迁移。按需先 `git revert` M7，再依次回退 M6 `99e8e13`、M5b `cb8f13b`、M5a `ae9c277`、M4 `6755a7d`、M3 `dcb73c9`、M2 `7b5fcd6`、M1 `bace223`；不使用硬重置，不覆盖用户未提交文件。M0 文档可保留。

数据库全链路和真实模型质量仍待独立环境验证；T01 浏览器历史失败并未由目录迁移解决。单进程、检查点路径、节点名、状态、thread_id、审批及预算契约保持原样。物流后续任务仍以 [任务状态](../logistics-execution/task-status.md) 为准。
