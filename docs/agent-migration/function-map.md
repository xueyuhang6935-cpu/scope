# 逐函数迁移清单

本清单补充 [职责与副作用映射](file-map.md)，不以类名或方法组代替函数条目。运行代码以迁移起点 `6669e8d` 为旧版本、交付补全时的 `07ecda0` 为新版本；测试以 M6 前 `cb8f13b` 为旧版本。每行列出一个具名函数/方法（包括嵌套函数），拆分目标逐个列出实际定义。

覆盖范围：原八个迁移运行模块的 **68 个函数**，以及六个搬迁测试模块的 **93 个函数**。未发生迁移的其他业务模块不在此清单内；M1—M6 新增专项测试见目录及阶段记录，不冒充旧函数搬迁。匿名 lambda、类/状态定义、常量、模板和 JSON 数据不是具名函数，仍见 file-map.md 的对象映射。

行号对应上述版本，仅辅助定位；路径和完整限定函数名是主要标识。目标定义通过 AST 检查存在性，并反向检查涉及的新运行模块无遗漏定义。显式 import 转发不产生新的函数定义，因此列出其真实定义位置；转发与宿主保留原因见 [兼容入口](compatibility.md)。

## backend/supply_prompts.py

| 旧位置（6669e8d） | 当前定义位置 | 关系 |
| --- | --- | --- |
| `backend/supply_prompts.py:load_prompt`（行 6） | `project/prompts/loader.py:load_prompt`（行 6） | 迁移；核心单一实现，旧模块显式转发。 |

## backend/supply_context.py

| 旧位置（6669e8d） | 当前定义位置 | 关系 |
| --- | --- | --- |
| `backend/supply_context.py:requested_action`（行 11） | `backend/supply_context.py:requested_action`（行 12） | 保留后端业务意图/追问规则。 |
| `backend/supply_context.py:compact_task`（行 30） | `project/context/investigation.py:compact_task`（行 10） | 迁移；核心单一实现，旧模块显式转发。 |
| `backend/supply_context.py:compact_task.relevant`（行 35） | `project/context/investigation.py:compact_task.relevant`（行 15） | 随外层函数迁移，局部作用域保留。 |
| `backend/supply_context.py:compact_task.relevant.matches`（行 37） | `project/context/investigation.py:compact_task.relevant.matches`（行 17） | 随外层函数迁移，局部作用域保留。 |
| `backend/supply_context.py:focus_context`（行 62） | `project/context/investigation.py:focus_context`（行 42） | 迁移；核心单一实现，旧模块显式转发。 |
| `backend/supply_context.py:followup`（行 94） | `backend/supply_context.py:followup`（行 31） | 保留后端业务意图/追问规则。 |
| `backend/supply_context.py:grounded_answer`（行 119） | `project/context/investigation.py:grounded_answer`（行 74） | 迁移；核心单一实现，旧模块显式转发。 |

## backend/investigation_report.py

| 旧位置（6669e8d） | 当前定义位置 | 关系 |
| --- | --- | --- |
| `backend/investigation_report.py:validate_report`（行 26） | `project/agents/report_protocol.py:validate_report`（行 26） | 迁移；核心单一实现，旧模块显式转发。 |
| `backend/investigation_report.py:plan_blockers`（行 66） | `backend/investigation_report.py:plan_blockers`（行 5） | 保留后端确定性业务方案阻断。 |

## backend/investigation_sql.py

| 旧位置（6669e8d） | 当前定义位置 | 关系 |
| --- | --- | --- |
| `backend/investigation_sql.py:validate_sql`（行 55） | `project/tools/investigation_sql.py:validate_sql`（行 53） | 迁移；核心单一实现，旧模块显式转发。 |
| `backend/investigation_sql.py:validate_sql.parameter_name`（行 89） | `project/tools/investigation_sql.py:validate_sql.parameter_name`（行 87） | 局部占位符解析随 validate_sql 原样迁移。 |
| `backend/investigation_sql.py:InvestigationSQL.__init__`（行 111） | `project/tools/investigation_sql.py:InvestigationSQL.__init__`（行 117）<br>`backend/investigation_sql.py:InvestigationSQL.__init__`（行 37）<br>`backend/investigation_sql.py:restricted_connection`（行 15） | 拆分：核心接收 connect/authorize；原构造接口由宿主注入受限连接，连接配置及事务留后端。 |
| `backend/investigation_sql.py:InvestigationSQL.call`（行 115） | `project/tools/investigation_sql.py:InvestigationSQL.call`（行 121）<br>`backend/investigation_sql.py:InvestigationSQL.__init__`（行 37）<br>`backend/investigation_sql.py:restricted_connection`（行 15） | 拆分：核心接收 connect/authorize；原构造接口由宿主注入受限连接，连接配置及事务留后端。 |

## backend/investigation_harness.py

| 旧位置（6669e8d） | 当前定义位置 | 关系 |
| --- | --- | --- |
| `backend/investigation_harness.py:InvestigationHarness.__init__`（行 10） | `project/tools/executor.py:InvestigationHarness.__init__`（行 9）<br>`backend/investigation_audit.py:InvestigationAuditStore.__init__`（行 6）<br>`backend/investigation_harness.py:InvestigationHarness.__init__`（行 7） | 拆分：生命周期/权限由 executor 执行，持久化由注入的审计存储执行。 后端保留旧组装/中断入口。 |
| `backend/investigation_harness.py:InvestigationHarness.check`（行 19） | `project/tools/executor.py:InvestigationHarness.check`（行 12） | 拆分：生命周期/权限由 executor 执行，持久化由注入的审计存储执行。 |
| `backend/investigation_harness.py:InvestigationHarness.start`（行 43） | `project/tools/executor.py:InvestigationHarness.start`（行 36）<br>`backend/investigation_audit.py:InvestigationAuditStore.start`（行 15） | 拆分：生命周期/权限由 executor 执行，持久化由注入的审计存储执行。 |
| `backend/investigation_harness.py:InvestigationHarness.finish`（行 52） | `project/tools/executor.py:InvestigationHarness.finish`（行 41）<br>`backend/investigation_audit.py:InvestigationAuditStore.finish`（行 22） | 拆分：生命周期/权限由 executor 执行，持久化由注入的审计存储执行。 |
| `backend/investigation_harness.py:InvestigationHarness.set_scope`（行 64） | `project/tools/executor.py:InvestigationHarness.set_scope`（行 47）<br>`backend/investigation_audit.py:InvestigationAuditStore.set_scope`（行 31） | 拆分：生命周期/权限由 executor 执行，持久化由注入的审计存储执行。 |
| `backend/investigation_harness.py:InvestigationHarness.interrupt`（行 70） | `backend/investigation_audit.py:InvestigationAuditStore.interrupt`（行 37）<br>`backend/investigation_harness.py:InvestigationHarness.interrupt`（行 11） | 拆分：生命周期/权限由 executor 执行，持久化由注入的审计存储执行。 后端保留旧组装/中断入口。 |
| `backend/investigation_harness.py:InvestigationHarness.records`（行 74） | `project/tools/executor.py:InvestigationHarness.records`（行 50）<br>`backend/investigation_audit.py:InvestigationAuditStore.records`（行 41） | 拆分：生命周期/权限由 executor 执行，持久化由注入的审计存储执行。 |

## backend/investigation_agent.py

| 旧位置（6669e8d） | 当前定义位置 | 关系 |
| --- | --- | --- |
| `backend/investigation_agent.py:investigate`（行 16） | `project/agents/investigator.py:investigate`（行 12）<br>`project/context/investigation.py:initial_report`（行 99）<br>`project/context/investigation.py:investigation_messages`（行 116）<br>`project/tools/investigation_dispatch.py:InvestigationTools.__init__`（行 8）<br>`project/tools/investigation_dispatch.py:InvestigationTools.call`（行 22）<br>`backend/investigation_model.py:model_session`（行 10）<br>`backend/investigation_model.py:model_session.request`（行 12）<br>`backend/investigation_agent.py:investigate`（行 7） | 拆分：判断循环迁入 agents；报告/消息、工具分发、HTTP 分别抽取；旧签名留宿主组装。 |
| `backend/investigation_agent.py:investigate.stop`（行 39） | `project/agents/investigator.py:investigate.stop`（行 18） | 局部停止/预算/反馈/修正函数随循环迁移，不成为后端重复实现。 |
| `backend/investigation_agent.py:investigate.budget`（行 68） | `project/agents/investigator.py:investigate.budget`（行 46） | 局部停止/预算/反馈/修正函数随循环迁移，不成为后端重复实现。 |
| `backend/investigation_agent.py:investigate.feedback`（行 113） | `project/agents/investigator.py:investigate.feedback`（行 78） | 局部停止/预算/反馈/修正函数随循环迁移，不成为后端重复实现。 |
| `backend/investigation_agent.py:investigate.repair`（行 124） | `project/agents/investigator.py:investigate.repair`（行 89） | 局部停止/预算/反馈/修正函数随循环迁移，不成为后端重复实现。 |

## backend/supply_workflow.py

| 旧位置（6669e8d） | 当前定义位置 | 关系 |
| --- | --- | --- |
| `backend/supply_workflow.py:SupplyWorkflow.__init__`（行 41） | `backend/supply_workflow.py:SupplyWorkflow.__init__`（行 12）<br>`backend/investigation_service.py:InvestigationService.__init__`（行 22）<br>`project/workflows/investigation.py:InvestigationWorkflow.__init__`（行 22）<br>`backend/supply_workflow.py:SupplyWorkflow.schedule`（行 21） | 拆分：服务状态初始化、图/执行器、检查点资源分别归属；schedule 抽出提交后调度。 |
| `backend/supply_workflow.py:SupplyWorkflow.ensure`（行 72） | `backend/investigation_service.py:InvestigationService.ensure`（行 39） | 迁入业务服务；SupplyWorkflow 继承保留原方法访问，授权/事务语义不变。 |
| `backend/supply_workflow.py:SupplyWorkflow.access`（行 84） | `backend/investigation_service.py:InvestigationService.access`（行 51） | 迁入业务服务；SupplyWorkflow 继承保留原方法访问，授权/事务语义不变。 |
| `backend/supply_workflow.py:SupplyWorkflow.user_by_account`（行 91） | `backend/investigation_service.py:InvestigationService.user_by_account`（行 58） | 迁入业务服务；SupplyWorkflow 继承保留原方法访问，授权/事务语义不变。 |
| `backend/supply_workflow.py:SupplyWorkflow.config_for`（行 97） | `backend/investigation_service.py:InvestigationService.config_for`（行 64） | 迁入业务服务；SupplyWorkflow 继承保留原方法访问，授权/事务语义不变。 |
| `backend/supply_workflow.py:SupplyWorkflow.run_record`（行 103） | `backend/investigation_service.py:InvestigationService.run_record`（行 70） | 迁入业务服务；SupplyWorkflow 继承保留原方法访问，授权/事务语义不变。 |
| `backend/supply_workflow.py:SupplyWorkflow.event`（行 109） | `backend/investigation_service.py:InvestigationService.event`（行 76） | 迁入业务服务；SupplyWorkflow 继承保留原方法访问，授权/事务语义不变。 |
| `backend/supply_workflow.py:SupplyWorkflow.collect`（行 115） | `project/workflows/investigation.py:InvestigationWorkflow.collect`（行 36）<br>`backend/investigation_service.py:InvestigationService.collect_data`（行 82）<br>`backend/supply_workflow.py:SupplyWorkflow.collect`（行 34） | 拆分：节点编排归 workflows，业务体归服务，旧节点方法仅委托。 |
| `backend/supply_workflow.py:SupplyWorkflow.authorize_investigation`（行 145） | `backend/investigation_service.py:InvestigationService.authorize_investigation`（行 111） | 迁入业务服务；SupplyWorkflow 继承保留原方法访问，授权/事务语义不变。 |
| `backend/supply_workflow.py:SupplyWorkflow.investigate`（行 161） | `project/workflows/investigation.py:InvestigationWorkflow.investigate`（行 40）<br>`backend/investigation_service.py:InvestigationService.run_investigation`（行 127）<br>`backend/supply_workflow.py:SupplyWorkflow.investigate`（行 37） | 拆分：节点编排归 workflows，业务体归服务，旧节点方法仅委托。 |
| `backend/supply_workflow.py:SupplyWorkflow.read_evidence`（行 188） | `backend/investigation_service.py:InvestigationService.read_evidence`（行 153） | 迁入业务服务；SupplyWorkflow 继承保留原方法访问，授权/事务语义不变。 |
| `backend/supply_workflow.py:SupplyWorkflow.plans`（行 223） | `project/workflows/investigation.py:InvestigationWorkflow.plans`（行 44）<br>`backend/investigation_service.py:InvestigationService.save_plans`（行 188）<br>`backend/supply_workflow.py:SupplyWorkflow.plans`（行 40） | 拆分：节点编排归 workflows，业务体归服务，旧节点方法仅委托。 |
| `backend/supply_workflow.py:SupplyWorkflow.approval`（行 258） | `project/workflows/investigation.py:InvestigationWorkflow.approval`（行 52）<br>`backend/supply_workflow.py:SupplyWorkflow.approval`（行 43） | 编排迁移，宿主保留委托；持久化/恢复查询抽入服务，close 先停执行器再关宿主检查点连接。 |
| `backend/supply_workflow.py:SupplyWorkflow.finish`（行 263） | `project/workflows/investigation.py:InvestigationWorkflow.finish`（行 48）<br>`backend/investigation_service.py:InvestigationService.finish_approval`（行 222）<br>`backend/supply_workflow.py:SupplyWorkflow.finish`（行 46） | 拆分：节点编排归 workflows，业务体归服务，旧节点方法仅委托。 |
| `backend/supply_workflow.py:SupplyWorkflow.replan`（行 275） | `backend/investigation_service.py:InvestigationService.replan`（行 233） | 迁入业务服务；SupplyWorkflow 继承保留原方法访问，授权/事务语义不变。 |
| `backend/supply_workflow.py:SupplyWorkflow.process`（行 326） | `project/workflows/investigation.py:InvestigationWorkflow.process`（行 57）<br>`backend/supply_workflow.py:SupplyWorkflow.process`（行 24）<br>`backend/investigation_service.py:InvestigationService.prepare_run`（行 606）<br>`backend/investigation_service.py:InvestigationService.fail_run`（行 615）<br>`backend/investigation_service.py:InvestigationService.interrupt_calls`（行 630） | 编排迁移，宿主保留委托；持久化/恢复查询抽入服务，close 先停执行器再关宿主检查点连接。 |
| `backend/supply_workflow.py:SupplyWorkflow.recover`（行 369） | `project/workflows/investigation.py:InvestigationWorkflow.recover`（行 96）<br>`backend/supply_workflow.py:SupplyWorkflow.recover`（行 27）<br>`backend/investigation_service.py:InvestigationService.has_recovery_config`（行 620）<br>`backend/investigation_service.py:InvestigationService.recoverable_runs`（行 623） | 编排迁移，宿主保留委托；持久化/恢复查询抽入服务，close 先停执行器再关宿主检查点连接。 |
| `backend/supply_workflow.py:SupplyWorkflow.close`（行 385） | `project/workflows/investigation.py:InvestigationWorkflow.close`（行 108）<br>`backend/supply_workflow.py:SupplyWorkflow.close`（行 30） | 编排迁移，宿主保留委托；持久化/恢复查询抽入服务，close 先停执行器再关宿主检查点连接。 |
| `backend/supply_workflow.py:SupplyWorkflow.validate_plan`（行 389） | `backend/investigation_service.py:InvestigationService.validate_plan`（行 284） | 迁入业务服务；SupplyWorkflow 继承保留原方法访问，授权/事务语义不变。 |
| `backend/supply_workflow.py:SupplyWorkflow.record_stage`（行 422） | `backend/investigation_service.py:InvestigationService.record_stage`（行 317） | 迁入业务服务；SupplyWorkflow 继承保留原方法访问，授权/事务语义不变。 |
| `backend/supply_workflow.py:SupplyWorkflow.read_run`（行 426） | `backend/investigation_service.py:InvestigationService.read_run`（行 321） | 迁入业务服务；SupplyWorkflow 继承保留原方法访问，授权/事务语义不变。 |
| `backend/supply_workflow.py:SupplyWorkflow.from_calculation`（行 443） | `backend/investigation_service.py:InvestigationService.from_calculation`（行 338） | 迁入业务服务；SupplyWorkflow 继承保留原方法访问，授权/事务语义不变。 |
| `backend/supply_workflow.py:SupplyWorkflow.start_investigation`（行 485） | `backend/investigation_service.py:InvestigationService.start_investigation`（行 380） | 迁入业务服务；SupplyWorkflow 继承保留原方法访问，授权/事务语义不变。 |
| `backend/supply_workflow.py:SupplyWorkflow.routes`（行 527） | `backend/investigation_routes.py:routes`（行 5）<br>`backend/investigation_service.py:InvestigationService.routes`（行 601） | 拆出路由注册；旧 routes 方法由继承的服务方法委托，main 直接挂载新模块。 |
| `backend/supply_workflow.py:SupplyWorkflow.routes.read_run`（行 531） | `backend/investigation_routes.py:routes.read_run`（行 9）<br>`backend/investigation_service.py:InvestigationService.read_run`（行 321） | 拆分：薄端点保留请求/身份取值；服务方法执行原业务检查、SQL 与事务。 |
| `backend/supply_workflow.py:SupplyWorkflow.routes.access_info`（行 536） | `backend/investigation_routes.py:routes.access_info`（行 14）<br>`backend/investigation_service.py:InvestigationService.access_info`（行 422） | 拆分：薄端点保留请求/身份取值；服务方法执行原业务检查、SQL 与事务。 |
| `backend/supply_workflow.py:SupplyWorkflow.routes.set_member`（行 559） | `backend/investigation_routes.py:routes.set_member`（行 19）<br>`backend/investigation_service.py:InvestigationService.set_member`（行 443） | 拆分：薄端点保留请求/身份取值；服务方法执行原业务检查、SQL 与事务。 |
| `backend/supply_workflow.py:SupplyWorkflow.routes.start`（行 576） | `backend/investigation_routes.py:routes.start`（行 24）<br>`backend/investigation_service.py:InvestigationService.start_investigation`（行 380） | 拆分：薄端点保留请求/身份取值；服务方法执行原业务检查、SQL 与事务。 |
| `backend/supply_workflow.py:SupplyWorkflow.routes.runs`（行 581） | `backend/investigation_routes.py:routes.runs`（行 29）<br>`backend/investigation_service.py:InvestigationService.runs`（行 457） | 拆分：薄端点保留请求/身份取值；服务方法执行原业务检查、SQL 与事务。 |
| `backend/supply_workflow.py:SupplyWorkflow.routes.tool_calls`（行 597） | `backend/investigation_routes.py:routes.tool_calls`（行 34）<br>`backend/investigation_service.py:InvestigationService.tool_calls`（行 471） | 拆分：薄端点保留请求/身份取值；服务方法执行原业务检查、SQL 与事务。 |
| `backend/supply_workflow.py:SupplyWorkflow.routes.cancel`（行 617） | `backend/investigation_routes.py:routes.cancel`（行 39）<br>`backend/investigation_service.py:InvestigationService.cancel`（行 489） | 拆分：薄端点保留请求/身份取值；服务方法执行原业务检查、SQL 与事务。 |
| `backend/supply_workflow.py:SupplyWorkflow.routes.approval_list`（行 636） | `backend/investigation_routes.py:routes.approval_list`（行 44）<br>`backend/investigation_service.py:InvestigationService.approval_list`（行 506） | 拆分：薄端点保留请求/身份取值；服务方法执行原业务检查、SQL 与事务。 |
| `backend/supply_workflow.py:SupplyWorkflow.routes.shared_events`（行 656） | `backend/investigation_routes.py:routes.shared_events`（行 49）<br>`backend/investigation_service.py:InvestigationService.shared_events`（行 524） | 拆分：薄端点保留请求/身份取值；服务方法执行原业务检查、SQL 与事务。 |
| `backend/supply_workflow.py:SupplyWorkflow.routes.plan_detail`（行 669） | `backend/investigation_routes.py:routes.plan_detail`（行 54）<br>`backend/investigation_service.py:InvestigationService.plan_detail`（行 535） | 拆分：薄端点保留请求/身份取值；服务方法执行原业务检查、SQL 与事务。 |
| `backend/supply_workflow.py:SupplyWorkflow.routes.decide`（行 697） | `backend/investigation_routes.py:routes.decide`（行 59）<br>`backend/investigation_service.py:InvestigationService.decide`（行 561） | 拆分：薄端点保留请求/身份取值；服务方法执行原业务检查、SQL 与事务。 |

## backend/eval_supply_agent.py

| 旧位置（6669e8d） | 当前定义位置 | 关系 |
| --- | --- | --- |
| `backend/eval_supply_agent.py:FixtureQueries.__init__`（行 24） | `project/evals/supply_agent.py:FixtureQueries.__init__`（行 19） | 迁入评测核心；固定样本、模拟响应或评分/审核语义保留。 |
| `backend/eval_supply_agent.py:FixtureQueries.call`（行 38） | `project/evals/supply_agent.py:FixtureQueries.call`（行 33） | 迁入评测核心；固定样本、模拟响应或评分/审核语义保留。 |
| `backend/eval_supply_agent.py:report_arguments`（行 66） | `project/evals/supply_agent.py:report_arguments`（行 61） | 迁入评测核心；固定样本、模拟响应或评分/审核语义保留。 |
| `backend/eval_supply_agent.py:offline_handler`（行 76） | `project/evals/supply_agent.py:offline_handler`（行 71） | 迁入评测核心；固定样本、模拟响应或评分/审核语义保留。 |
| `backend/eval_supply_agent.py:offline_handler.respond`（行 79） | `project/evals/supply_agent.py:offline_handler.respond`（行 74） | 迁入评测核心；固定样本、模拟响应或评分/审核语义保留。 |
| `backend/eval_supply_agent.py:evaluate`（行 113） | `project/evals/supply_agent.py:evaluate`（行 108）<br>`backend/eval_supply_agent.py:evaluate`（行 22）<br>`backend/eval_supply_agent.py:model_context`（行 15） | 拆分：纯评测流程归 evals，配置或模型会话适配归后端；原 CLI/调用签名保留。 |
| `backend/eval_supply_agent.py:main`（行 156） | `project/evals/supply_agent.py:main`（行 150）<br>`backend/eval_supply_agent.py:main`（行 33）<br>`backend/eval_supply_agent.py:load_live_env`（行 27） | 拆分：纯评测流程归 evals，配置或模型会话适配归后端；原 CLI/调用签名保留。 |

## 新增异常适配函数

| 当前定义 | 原代码来源 | 关系 |
| --- | --- | --- |
| `project/tools/investigation_sql.py:DatabaseQueryError.__init__`（行 111） | `backend/investigation_sql.py:InvestigationSQL.call` 的驱动异常处理 | 新增适配异常，仅传 SQLSTATE，替代核心直接依赖 psycopg 异常。 |

## M6 测试逐函数搬迁

以下包含测试方法、setUp/tearDown、辅助函数及局部模拟回调；不为旧路径创建重复发现包装。

| 旧位置（cb8f13b） | 当前定义位置 | 关系 |
| --- | --- | --- |
| `backend/test_investigation_adapters.py:AdapterTests.test_dedicated_connection_required`（行 16） | `project/tests/integration/test_investigation_adapters.py:AdapterTests.test_dedicated_connection_required`（行 16） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_adapters.py:AdapterTests.test_connection_settings_role_and_driver_errors`（行 23） | `project/tests/integration/test_investigation_adapters.py:AdapterTests.test_connection_settings_role_and_driver_errors`（行 23） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_adapters.py:AdapterTests.test_audit_restart_preserves_scope_and_other_runs`（行 59） | `project/tests/integration/test_investigation_adapters.py:AdapterTests.test_audit_restart_preserves_scope_and_other_runs`（行 59） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_harness.py:HarnessTests.setUp`（行 17） | `project/tests/integration/test_investigation_harness.py:HarnessTests.setUp`（行 17） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_harness.py:HarnessTests.setUp.authorize`（行 26） | `project/tests/integration/test_investigation_harness.py:HarnessTests.setUp.authorize`（行 26） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_harness.py:HarnessTests.tearDown`（行 32） | `project/tests/integration/test_investigation_harness.py:HarnessTests.tearDown`（行 32） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_harness.py:HarnessTests.agent`（行 36） | `project/tests/integration/test_investigation_harness.py:HarnessTests.agent`（行 36） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_harness.py:HarnessTests.agent.QueryTools.call`（行 39） | `project/tests/integration/test_investigation_harness.py:HarnessTests.agent.QueryTools.call`（行 39） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_harness.py:HarnessTests.agent.handler`（行 46） | `project/tests/integration/test_investigation_harness.py:HarnessTests.agent.handler`（行 46） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_harness.py:HarnessTests.test_revocation_before_cached_plan`（行 76） | `project/tests/integration/test_investigation_harness.py:HarnessTests.test_revocation_before_cached_plan`（行 76） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_harness.py:HarnessTests.test_revocation_before_report`（行 79） | `project/tests/integration/test_investigation_harness.py:HarnessTests.test_revocation_before_report`（行 79） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_harness.py:HarnessTests.test_domain_shrink_before_report`（行 82） | `project/tests/integration/test_investigation_harness.py:HarnessTests.test_domain_shrink_before_report`（行 82） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_harness.py:HarnessTests.test_factory_shrink_before_plan`（行 85） | `project/tests/integration/test_investigation_harness.py:HarnessTests.test_factory_shrink_before_plan`（行 85） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_harness.py:HarnessTests.test_range_change_stops_next_tool`（行 88） | `project/tests/integration/test_investigation_harness.py:HarnessTests.test_range_change_stops_next_tool`（行 88） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_harness.py:HarnessTests.test_completed_query_survives_restart_and_pending_is_interrupted`（行 91） | `project/tests/integration/test_investigation_harness.py:HarnessTests.test_completed_query_survives_restart_and_pending_is_interrupted`（行 91） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_model.py:ModelTransportTests.test_request_contract_and_session_cleanup`（行 13） | `project/tests/integration/test_investigation_model.py:ModelTransportTests.test_request_contract_and_session_cleanup`（行 13） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_model.py:ModelTransportTests.test_request_contract_and_session_cleanup.handler`（行 16） | `project/tests/integration/test_investigation_model.py:ModelTransportTests.test_request_contract_and_session_cleanup.handler`（行 16） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_model.py:ModelTransportTests.test_response_byte_and_time_limits_close_without_retry`（行 32） | `project/tests/integration/test_investigation_model.py:ModelTransportTests.test_response_byte_and_time_limits_close_without_retry`（行 32） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_model.py:ModelTransportTests.test_response_byte_and_time_limits_close_without_retry.handler`（行 37） | `project/tests/integration/test_investigation_model.py:ModelTransportTests.test_response_byte_and_time_limits_close_without_retry.handler`（行 37） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_model.py:ModelTransportTests.test_invalid_config_does_not_create_http_client`（行 50） | `project/tests/integration/test_investigation_model.py:ModelTransportTests.test_invalid_config_does_not_create_http_client`（行 50） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_routes.py:RouteTests.test_routes_delegate_authenticated_user_and_request_fields`（行 22） | `project/tests/integration/test_investigation_routes.py:RouteTests.test_routes_delegate_authenticated_user_and_request_fields`（行 22） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_routes.py:RouteTests.test_routes_delegate_authenticated_user_and_request_fields.body`（行 30） | `project/tests/integration/test_investigation_routes.py:RouteTests.test_routes_delegate_authenticated_user_and_request_fields.body`（行 30） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_routes.py:RouteTests.test_routes_delegate_authenticated_user_and_request_fields.api_error`（行 35） | `project/tests/integration/test_investigation_routes.py:RouteTests.test_routes_delegate_authenticated_user_and_request_fields.api_error`（行 35） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_routes.py:ServiceTests.setUp`（行 70） | `project/tests/integration/test_investigation_routes.py:ServiceTests.setUp`（行 70） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_routes.py:ServiceTests.setUp.execute`（行 89） | `project/tests/integration/test_investigation_routes.py:ServiceTests.setUp.execute`（行 89） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_routes.py:ServiceTests.setUp.connection`（行 96） | `project/tests/integration/test_investigation_routes.py:ServiceTests.setUp.connection`（行 96） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_routes.py:ServiceTests.test_approval_rejects_self_and_stale_versions`（行 110） | `project/tests/integration/test_investigation_routes.py:ServiceTests.test_approval_rejects_self_and_stale_versions`（行 110） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_routes.py:ServiceTests.test_committed_approval_precedes_resume_scheduling`（行 119） | `project/tests/integration/test_investigation_routes.py:ServiceTests.test_committed_approval_precedes_resume_scheduling`（行 119） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_routes.py:ServiceTests.test_repeated_approval_is_idempotent_and_conflicts_are_rejected`（行 126） | `project/tests/integration/test_investigation_routes.py:ServiceTests.test_repeated_approval_is_idempotent_and_conflicts_are_rejected`（行 126） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_routes.py:ServiceTests.test_stale_plan_commits_replan_before_returning_conflict`（行 135） | `project/tests/integration/test_investigation_routes.py:ServiceTests.test_stale_plan_commits_replan_before_returning_conflict`（行 135） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_routes.py:ServiceTests.test_cancel_does_not_race_a_graph_transition`（行 144） | `project/tests/integration/test_investigation_routes.py:ServiceTests.test_cancel_does_not_race_a_graph_transition`（行 144） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_routes.py:ServiceTests.test_model_reservation_commits_before_agent_and_replay_disallows_request`（行 152） | `project/tests/integration/test_investigation_routes.py:ServiceTests.test_model_reservation_commits_before_agent_and_replay_disallows_request`（行 152） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_routes.py:ServiceTests.test_model_reservation_commits_before_agent_and_replay_disallows_request.reserve`（行 159） | `project/tests/integration/test_investigation_routes.py:ServiceTests.test_model_reservation_commits_before_agent_and_replay_disallows_request.reserve`（行 159） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_routes.py:ServiceTests.test_model_reservation_commits_before_agent_and_replay_disallows_request.agent`（行 167） | `project/tests/integration/test_investigation_routes.py:ServiceTests.test_model_reservation_commits_before_agent_and_replay_disallows_request.agent`（行 167） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_routes.py:ServiceTests.test_finish_uses_persisted_approval_not_resume_payload`（行 183） | `project/tests/integration/test_investigation_routes.py:ServiceTests.test_finish_uses_persisted_approval_not_resume_payload`（行 183） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_routes.py:AssemblyTests.test_checkpoint_path_single_executor_and_close_order`（行 196） | `project/tests/integration/test_investigation_routes.py:AssemblyTests.test_checkpoint_path_single_executor_and_close_order`（行 196） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_sql.py:ValidationTests.test_select_join_cte_parameters`（行 19） | `project/tests/integration/test_investigation_sql.py:ValidationTests.test_select_join_cte_parameters`（行 19） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_sql.py:ValidationTests.test_escape_boundaries`（行 24） | `project/tests/integration/test_investigation_sql.py:ValidationTests.test_escape_boundaries`（行 24） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_sql.py:ValidationTests.test_literal_percent_and_placeholder`（行 43） | `project/tests/integration/test_investigation_sql.py:ValidationTests.test_literal_percent_and_placeholder`（行 43） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_sql.py:ValidationTests.test_query_error_repair_and_evidence`（行 49） | `project/tests/integration/test_investigation_sql.py:ValidationTests.test_query_error_repair_and_evidence`（行 49） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_sql.py:ValidationTests.test_query_error_repair_and_evidence.QueryTools.call`（行 52） | `project/tests/integration/test_investigation_sql.py:ValidationTests.test_query_error_repair_and_evidence.QueryTools.call`（行 52） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_sql.py:ValidationTests.test_query_error_repair_and_evidence.handler`（行 63） | `project/tests/integration/test_investigation_sql.py:ValidationTests.test_query_error_repair_and_evidence.handler`（行 63） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_sql.py:ValidationTests.test_schema_gate_and_cte_provenance`（行 78） | `project/tests/integration/test_investigation_sql.py:ValidationTests.test_schema_gate_and_cte_provenance`（行 78） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_sql.py:DatabaseTests.setUp`（行 91） | `project/tests/integration/test_investigation_sql.py:DatabaseTests.setUp`（行 91） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_sql.py:DatabaseTests.query`（行 100） | `project/tests/integration/test_investigation_sql.py:DatabaseTests.query`（行 100） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_sql.py:DatabaseTests.test_real_query_scope_and_schema`（行 103） | `project/tests/integration/test_investigation_sql.py:DatabaseTests.test_real_query_scope_and_schema`（行 103） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_sql.py:DatabaseTests.test_database_enforces_readonly_and_scope_without_model_filters`（行 120） | `project/tests/integration/test_investigation_sql.py:DatabaseTests.test_database_enforces_readonly_and_scope_without_model_filters`（行 120） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_sql.py:DatabaseTests.test_domain_revocation_truncation_and_error_recovery`（行 131） | `project/tests/integration/test_investigation_sql.py:DatabaseTests.test_domain_revocation_truncation_and_error_recovery`（行 131） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_sql.py:DatabaseTests.test_domain_revocation_truncation_and_error_recovery.revoked`（行 142） | `project/tests/integration/test_investigation_sql.py:DatabaseTests.test_domain_revocation_truncation_and_error_recovery.revoked`（行 142） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_investigation_sql.py:DatabaseTests.test_workspace_injected_changes_and_asof_isolation`（行 147） | `project/tests/integration/test_investigation_sql.py:DatabaseTests.test_workspace_injected_changes_and_asof_isolation`（行 147） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:WorkflowTests.setUp`（行 22） | `project/tests/integration/test_supply_workflow.py:WorkflowTests.setUp`（行 22） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:WorkflowTests.tearDown`（行 33） | `project/tests/integration/test_supply_workflow.py:WorkflowTests.tearDown`（行 33） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:WorkflowTests.test_planning_oracles`（行 44） | `project/tests/integration/test_supply_workflow.py:WorkflowTests.test_planning_oracles`（行 44） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:WorkflowTests.test_chat_query_explain_refresh_investigate_simulate_and_approval`（行 83） | `project/tests/integration/test_supply_workflow.py:WorkflowTests.test_chat_query_explain_refresh_investigate_simulate_and_approval`（行 83） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:WorkflowTests.test_chat_query_explain_refresh_investigate_simulate_and_approval.message`（行 87） | `project/tests/integration/test_supply_workflow.py:WorkflowTests.test_chat_query_explain_refresh_investigate_simulate_and_approval.message`（行 87） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:WorkflowTests.test_chat_simulation_replans_when_saved_candidates_are_stale`（行 149） | `project/tests/integration/test_supply_workflow.py:WorkflowTests.test_chat_simulation_replans_when_saved_candidates_are_stale`（行 149） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:WorkflowTests.test_chat_simulation_replans_when_saved_candidates_are_stale.query`（行 152） | `project/tests/integration/test_supply_workflow.py:WorkflowTests.test_chat_simulation_replans_when_saved_candidates_are_stale.query`（行 152） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:WorkflowTests.test_chat_old_snapshot_cannot_launch_investigation`（行 183） | `project/tests/integration/test_supply_workflow.py:WorkflowTests.test_chat_old_snapshot_cannot_launch_investigation`（行 183） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:WorkflowTests.start_run`（行 199） | `project/tests/integration/test_supply_workflow.py:WorkflowTests.start_run`（行 199） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:WorkflowTests.grant`（行 212） | `project/tests/integration/test_supply_workflow.py:WorkflowTests.grant`（行 212） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:WorkflowTests.wait_child`（行 216） | `project/tests/integration/test_supply_workflow.py:WorkflowTests.wait_child`（行 216） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:WorkflowTests.reject`（行 225） | `project/tests/integration/test_supply_workflow.py:WorkflowTests.reject`（行 225） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:WorkflowTests.test_replan_limit_stops_chain`（行 230） | `project/tests/integration/test_supply_workflow.py:WorkflowTests.test_replan_limit_stops_chain`（行 230） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:WorkflowTests.test_replan_stops_when_all_alternatives_rejected`（行 246） | `project/tests/integration/test_supply_workflow.py:WorkflowTests.test_replan_stops_when_all_alternatives_rejected`（行 246） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:WorkflowTests.test_stale_plan_read_automatically_replans`（行 258） | `project/tests/integration/test_supply_workflow.py:WorkflowTests.test_stale_plan_read_automatically_replans`（行 258） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:WorkflowTests.test_recover_pending_replan_and_revoked_requester`（行 267） | `project/tests/integration/test_supply_workflow.py:WorkflowTests.test_recover_pending_replan_and_revoked_requester`（行 267） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:WorkflowTests.test_durable_approval_and_permissions`（行 285） | `project/tests/integration/test_supply_workflow.py:WorkflowTests.test_durable_approval_and_permissions`（行 285） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:WorkflowTests.test_sql_model_workflow_persists_evidence_and_trace`（行 322） | `project/tests/integration/test_supply_workflow.py:WorkflowTests.test_sql_model_workflow_persists_evidence_and_trace`（行 322） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:WorkflowTests.test_sql_model_workflow_persists_evidence_and_trace.handler`（行 324） | `project/tests/integration/test_supply_workflow.py:WorkflowTests.test_sql_model_workflow_persists_evidence_and_trace.handler`（行 324） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:WorkflowTests.test_changed_snapshot_and_revocation`（行 353） | `project/tests/integration/test_supply_workflow.py:WorkflowTests.test_changed_snapshot_and_revocation`（行 353） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:WorkflowTests.test_report_save_rechecks_domain_and_scope`（行 366） | `project/tests/integration/test_supply_workflow.py:WorkflowTests.test_report_save_rechecks_domain_and_scope`（行 366） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:WorkflowTests.test_evidence_reader_checks_snapshot_membership_and_revocation`（行 383） | `project/tests/integration/test_supply_workflow.py:WorkflowTests.test_evidence_reader_checks_snapshot_membership_and_revocation`（行 383） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:WorkflowTests.test_factory_scope_rejection_and_replan`（行 414） | `project/tests/integration/test_supply_workflow.py:WorkflowTests.test_factory_scope_rejection_and_replan`（行 414） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:report_args`（行 450） | `project/tests/integration/test_agent_loop.py:report_args`（行 11） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:AgentTests.run_agent`（行 457） | `project/tests/integration/test_agent_loop.py:AgentTests.run_agent`（行 18） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:AgentTests.run_agent.handler`（行 463） | `project/tests/integration/test_agent_loop.py:AgentTests.run_agent.handler`（行 24） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:AgentTests.test_cited_tool_report_and_unknown_usage`（行 477） | `project/tests/integration/test_agent_loop.py:AgentTests.test_cited_tool_report_and_unknown_usage`（行 38） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:AgentTests.test_fabricated_evidence_budget_and_replay`（行 485） | `project/tests/integration/test_agent_loop.py:AgentTests.test_fabricated_evidence_budget_and_replay`（行 46） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:AgentTests.test_invalid_report_replans_and_reads_missing_evidence`（行 498） | `project/tests/integration/test_agent_loop.py:AgentTests.test_invalid_report_replans_and_reads_missing_evidence`（行 59） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:AgentTests.test_invalid_tool_is_corrected_without_execution`（行 511） | `project/tests/integration/test_agent_loop.py:AgentTests.test_invalid_tool_is_corrected_without_execution`（行 72） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:AgentTests.test_replanning_feedback_reaches_model`（行 519） | `project/tests/integration/test_agent_loop.py:AgentTests.test_replanning_feedback_reaches_model`（行 80） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:AgentTests.test_transport_failure_is_not_retried`（行 525） | `project/tests/integration/test_agent_loop.py:AgentTests.test_transport_failure_is_not_retried`（行 86） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:AgentTests.test_transport_failure_is_not_retried.handler`（行 527） | `project/tests/integration/test_agent_loop.py:AgentTests.test_transport_failure_is_not_retried.handler`（行 88） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:AgentTests.test_plan_gate_is_enforced_even_when_model_ignores_tools`（行 537） | `project/tests/integration/test_agent_loop.py:AgentTests.test_plan_gate_is_enforced_even_when_model_ignores_tools`（行 98） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:AgentTests.test_partial_requires_reason_and_does_not_invalidate_feasible_plans`（行 547） | `project/tests/integration/test_agent_loop.py:AgentTests.test_partial_requires_reason_and_does_not_invalidate_feasible_plans`（行 108） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:AgentTests.test_claim_validation_and_legacy_fields_are_derived`（行 560） | `project/tests/integration/test_agent_loop.py:AgentTests.test_claim_validation_and_legacy_fields_are_derived`（行 121） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:AgentTests.test_final_round_can_submit_partial_without_evidence`（行 573） | `project/tests/integration/test_agent_loop.py:AgentTests.test_final_round_can_submit_partial_without_evidence`（行 134） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:AgentTests.test_near_token_budget_reserves_report_call`（行 581） | `project/tests/integration/test_agent_loop.py:AgentTests.test_near_token_budget_reserves_report_call`（行 142） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:AgentTests.test_time_budget_reserves_partial_report`（行 587） | `project/tests/integration/test_agent_loop.py:AgentTests.test_time_budget_reserves_partial_report`（行 148） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:AgentTests.test_transport_failure_preserves_completed_evidence`（行 595） | `project/tests/integration/test_agent_loop.py:AgentTests.test_transport_failure_preserves_completed_evidence`（行 156） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:AgentTests.test_snapshot_truncation_is_visible_to_model`（行 602） | `project/tests/integration/test_agent_loop.py:AgentTests.test_snapshot_truncation_is_visible_to_model`（行 163） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:AgentTests.test_evidence_expansion_and_candidate_results_are_citable`（行 611） | `project/tests/integration/test_agent_loop.py:AgentTests.test_evidence_expansion_and_candidate_results_are_citable`（行 172） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
| `backend/test_supply_workflow.py:AgentTests.test_evidence_expansion_and_candidate_results_are_citable.reader`（行 614） | `project/tests/integration/test_agent_loop.py:AgentTests.test_evidence_expansion_and_candidate_results_are_citable.reader`（行 175） | 原断言/辅助逻辑迁移；仅调整导入与必要文件根路径。 |
