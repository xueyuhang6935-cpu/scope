# 文件与函数迁移映射

核查起点：`6669e8d`。本次只执行 M1、M2 所列条目。未来阶段条目为职责边界，尚非已实施路径。

## 本批完整函数清单

| 原位置 / 对象 | 输入 → 输出；副作用 | 目标 / 处理 | 调用方 / 验证 |
| --- | --- | --- | --- |
| `supply_prompts.load_prompt(name)` | 名称 → 文本、内容版本；读取 UTF-8 模板 | M1：`project/prompts/loader.py`，按模块路径读取 | 模块初始化；模板字节、版本与不同工作目录测试 |
| `SCOPE_PROMPT / SCOPE_PROMPT_VERSION / INVESTIGATOR_PROMPT / INVESTIGATOR_PROMPT_VERSION` | 导入时读取两个模板 | M1：同上；旧模块显式转发 | `supply_model`、`investigation_agent`；离线评测 |
| `prompts/*.txt` 两模板 | 模型行为规则；无执行副作用 | M1：`project/prompts/`，内容不改 | 加载器及专题文档 |
| `supply_context.compact_task(row)` | 已授权持久化行 → 有界摘要；JSON 解析，无数据库访问 | M2：`project/context/investigation.py` | `supply.SupplyService.conversation_context` 及消息入口；焦点优先与长输出测试 |
| 内嵌 `relevant / matches` | 明细 → 焦点优先的前五项；无外部副作用 | 随 compact_task 原样迁移 | 同上 |
| `supply_context.focus_context(context, run=None)` | 当前摘要与已授权运行 → 补充事实、未解问题、拒绝历史；原地修改 context | M2：同上；保留原地更新语义 | `conversation_context/enrich_context`；旧报告、反证、截断与快照对照 |
| `supply_context.grounded_answer(context)` | 保存结果 → 解释字符串；不重查 | M2：同上 | `supply` 的规则回答；失败/无结果/旧结果测试 |
| `supply_context.ACTIONS / requested_action(text)` | 文本 → 业务动作；确定性匹配 | 本批留后端，属于业务意图规则 | `supply`、`supply_model`、followup；原有追问回归 |
| `supply_context.followup(text, scope, context)` | 问题/范围/上下文 → 解释标志、有效范围、新问题；依赖 supply_analysis.normalize_question/propose | 本批留后端；不能把供应链解析依赖引入 project | `supply`；工厂冲突、范围继承、刷新测试 |
| `investigation_report.CLAIM_SCHEMA / REPORT_SCHEMA` | 输出协议常量 | M2：`project/agents/report_protocol.py` | `investigation_agent` 工具声明；协议一致性 |
| `investigation_report.validate_report(args, evidence)` | 新报告与已读证据 → 规范报告或 ValueError；无写入 | M2：同上；原错误消息与派生旧字段不变 | `investigation_agent`、`test_supply_workflow`；伪造引用、截断、partial 及旧提交拒绝 |
| `investigation_report.plan_blockers(report, bundle)` | 报告/确定性方案 → 阻断理由；无写入 | 本批留后端，业务规则不迁移 | `supply_workflow.plans`；已有方案阻断测试 |

## 读取、授权和调用边界

`supply.SupplyService.conversation_context` 负责读 SQLite、核查每条历史记录权限及源快照一致性、选择六条近期摘要；`enrich_context/check_context` 读取调查并复核授权。这些方法留在后端，只有已授权数据传给 project/context。纯函数不接收数据库、请求对象或 `.env`。

`supply_model` 和 `investigation_agent` 的提示词导入改为新路径；M2 `supply` 对纯上下文函数、`investigation_agent` 对报告协议及相关测试也改为新路径。旧模块只为外部兼容显式转发已迁出的对象。现有 HTTP 模拟 patch 仍指向 `backend.supply`、`backend.investigation_agent.httpx.Client`，无需更改模型模块位置。

路径引用核查覆盖 README、docs、backend、tests、scripts；历史 `plan/` 与 T01 基线保留当时路径。当前专题说明随迁移更新。`backend.eval_supply_agent` 及案例路径本批保留，M6 再迁移。

## 后续混合模块的逐函数归属（本次不动）

| 模块 / 函数对象 | 未来归属及副作用 | 调用入口与验证 |
| --- | --- | --- |
| `SupplyWorkflow.__init__ / State` | 图与状态归 workflows；SQLite 打开、服务组装需留后端或传入 | main.create_app；启动和检查点兼容 |
| `ensure / config_for / run_record / event / user_by_account` | 后端服务；DDL、查询、事务 | 工作流及路由；数据库集成 |
| `access / authorize_investigation / read_evidence / validate_plan` | 后端授权及审批服务；查询、权限与版本校验 | 调查、工具及审批；越权/失效测试 |
| `collect / investigate / plans / approval / finish` | 阶段控制归 workflows；取数、模型、写入通过服务 | LangGraph；节点、人工等待和回执测试 |
| `replan / process / recover / close` | 编排与恢复归 workflows；执行器、数据库及资源清理先分离 | 启动恢复、工作线程；重启/重复动作测试 |
| `record_stage / read_run / from_calculation / start_investigation` | 后端调查服务；读写和幂等 | supply 与 routes；状态与创建测试 |
| `routes` 及内嵌端点 | 后端 API；请求授权、参数、响应 | FastAPI；接口回归 |
| `investigation_agent.investigate` 及内嵌 stop/budget/feedback/repair | M4 拆决策入 agents，上下文入 context，工具分发入 tools；模型请求及审计副作用需显式边界 | SupplyWorkflow.investigate、离线评测、模拟模型测试 |
| `InvestigationHarness.__init__ / start / finish / set_scope / interrupt / records` | M3 工具生命周期与后端审计存储分开；SQLite DDL/写入/读取 | 工作流、工具；审计生命周期测试 |
| `InvestigationHarness.check` | 工具调用保护，使用后端授权能力 | Agent 调用前后；撤权和范围变化测试 |
| `investigation_sql.validate_sql` | tools；纯 SQL 语法/白名单校验 | 工具调用及 SQL 单元测试 |
| `InvestigationSQL.__init__ / call` 及内嵌查询逻辑 | tools 与后端查询适配分开；数据库查询、超时、权限 | Agent/工作流；专用只读连接集成 |

## 明确保留

本批不迁移前端、FastAPI 入口、认证、数据库、schema、业务计算、SQL 工具、Agent 循环、工作流、既有测试位置及评测入口。`skills/`、`memory/` 等没有独立实现的目录仅说明职责，不引入新能力。M2 可以增加必要的迁移专项测试，不提前执行 M6 的测试整体搬迁。
