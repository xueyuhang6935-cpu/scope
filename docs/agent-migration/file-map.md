# 文件与函数迁移映射

核查起点：`6669e8d`。M1—M3 已完成，现追加 M4；后续阶段条目为职责边界，尚非已实施路径。

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

`supply_model` 和调查核心的提示词导入使用新路径；M2 `supply` 对纯上下文函数、调查核心对报告协议及相关测试也使用新路径。纯兼容对象显式转发；需要宿主能力的旧入口负责组装。M4 调查 HTTP 模拟 patch 改为 `backend.investigation_model.httpx.Client`，时钟 patch 为 `project.agents.investigator.time.monotonic`；范围解析的模拟入口保持不变。

路径引用核查覆盖 README、docs、backend、tests、scripts；历史 `plan/` 与 T01 基线保留当时路径。当前专题说明随迁移更新。`backend.eval_supply_agent` 及案例路径本批保留，M6 再迁移。

## M3 工具与调用保护

| 原对象 | 实际归属与副作用 | 调用方与验证 |
| --- | --- | --- |
| `investigation_sql` 白名单、`validate_sql` | `project/tools/investigation_sql.py`；纯 AST 校验 | Agent、安装器、离线评测改为直接导入；旧入口显式导出兼容对象 |
| Agent 内两个 SQL 工具声明 | 同上 `SQL_TOOL_SCHEMAS`；内容不变 | Agent 引用；与 M2 常量等价比较 |
| `InvestigationSQL.call` | 同上；通过注入连接执行 schema/查询，封装证据与错误 | 工作流继续使用后端兼容构造器；结构门禁、参数、截断、重复列、撤权测试 |
| DSN、连接与角色限制 | `backend/investigation_sql.restricted_connection`；打开 PostgreSQL、设置只读事务/超时/授权上下文 | 驱动错误转为仅携带 SQLSTATE 的异常；角色四项特权及专用连接测试 |
| `InvestigationHarness.check/start/finish/set_scope/records` | `project/tools/executor.py`；实时授权复核、生命周期，存储委托 | Agent 调用前后保护；旧撤权、范围变化及失败审计测试 |
| 审计表初始化、写入、读取与 `interrupt` | `backend/investigation_audit.py`；原 SQL 原样保留 | `backend/investigation_harness.py` 组装存储；临时 SQLite 重启、中断隔离与范围留存测试 |

## M4 Agent 判断循环

| 原对象 | 实际归属与副作用 | 调用方与验证 |
| --- | --- | --- |
| `investigation_agent.investigate`，内嵌 `stop/budget/feedback/repair` | `project/agents/investigator.py`；规划、预算、修正、停止、报告提交；调用注入能力 | 后端兼容组装入口；旧判断测试及新增直接注入测试 |
| 初始事实、来源陈述、报告与模型消息 | `project/context/investigation.py` 的 `initial_report/investigation_messages`；纯组装 | 判断循环；与旧表达式 AST 一致 |
| SQL/证据展开/缓存工具分发与已读集合 | `project/tools/investigation_dispatch.py` 的 `InvestigationTools`；通过已有工具/证据读取能力调用，按原顺序反馈、授权与登记 | 判断循环；schema 门禁、截断、证据引用及撤权测试 |
| HTTP 客户端、凭证、流式响应读取 | `backend/investigation_model.model_session`；注入返回请求函数的上下文管理器 | 后端组装；请求协议、资源释放、响应大小/时间限制、无重试检查 |
| 原 `backend.investigation_agent.investigate` 签名 | 保留兼容组装；实时配置可用性回调，过滤为模式及三项非密钥预算参数 | 工作流及评测继续经后端组装，不把配置或存储引入 project |

原有 backend 测试及 eval 数据没有整体移动，只调整模拟 patch 指向真正的 HTTP 适配器；M6 再处理测试与评测目录。M4 对照 M3 的 24 个固定案例（10 个 Agent、14 个范围解析），请求体和输出均一致。

## M5a API 与业务服务拆分

`backend/investigation_routes.py:routes(service)` 保留 11 个端点路径、名称、参数与响应，委托给 `backend/investigation_service.py:InvestigationService`。原嵌套路由中的成员管理、调查列表、审计读取、撤回、审批列表、方案详情和审批决定成为服务方法，保留原 SQL、权限、事务和提交后调度顺序。main 直接挂载新路由，旧工作流入口显式兼容转发。

## M5b 工作流与恢复待迁归属

| 模块 / 函数对象 | 未来归属及副作用 | 调用入口与验证 |
| --- | --- | --- |
| `SupplyWorkflow.__init__ / State` | 图与状态归 workflows；SQLite 打开、服务组装需留后端或传入 | main.create_app；启动和检查点兼容 |
| `ensure / config_for / run_record / event / user_by_account` | 后端服务；DDL、查询、事务 | 工作流及路由；数据库集成 |
| `access / authorize_investigation / read_evidence / validate_plan` | 后端授权及审批服务；查询、权限与版本校验 | 调查、工具及审批；越权/失效测试 |
| `collect / investigate / plans / approval / finish` | 阶段控制归 workflows；取数、模型、写入通过服务 | LangGraph；节点、人工等待和回执测试 |
| `replan / process / recover / close` | 编排与恢复归 workflows；执行器、数据库及资源清理先分离 | 启动恢复、工作线程；重启/重复动作测试 |
| `record_stage / read_run / from_calculation / start_investigation` | 后端调查服务；读写和幂等 | supply 与 routes；状态与创建测试 |
| `routes` 及内嵌端点 | 后端 API；请求授权、参数、响应 | FastAPI；接口回归 |

## 明确保留

本批不迁移前端、FastAPI 入口、认证、数据库、schema、业务计算、工作流、既有测试位置及评测入口。`skills/`、`memory/` 等没有独立实现的目录仅说明职责，不引入新能力。M4 增加必要的迁移专项测试，不提前执行 M6 的测试整体搬迁。
