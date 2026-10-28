# 独立外部能力

M3 已迁移：

- `investigation_sql.py`：两个 SQL 工具声明、表与函数白名单、SQL 校验、结构读取门禁、结果限量与证据封装、错误协议。`InvestigationSQL(connect, authorize)` 每次调用重新授权，使用 `connect(scope)` 提供的上下文管理器执行查询。
- `executor.py`：`InvestigationHarness(store, authorize)` 负责已读证据和候选方案的权限复核、调用编号、耗时及审计生命周期委托。
- M4 `investigation_dispatch.py`：`InvestigationTools` 保存本轮已读证据和已读表字段，分发 SQL、历史证据展开、预计算候选及快照读取；按原顺序复核权限、交付工具结果和登记证据。交付与修正回调由 Agent 提供，模型规划和报告决策不放入工具层。

后端 `investigation_sql.py` 保留旧构造接口并提供受限连接：专用 DSN、只读事务、超时、角色校验、授权上下文；驱动错误仅以 `DatabaseQueryError(sqlstate)` 传回工具。`investigation_audit.py` 负责 SQLite 审计表与读写，`investigation_harness.py` 负责组装及重启中断入口。无第二份工具实现。

本目录不读取环境配置、不创建数据库连接、不导入 backend/FastAPI。授权事实由后端实时计算，工具只执行现有保护规则。Agent 决策循环与预算已于 M4 迁入 `project/agents/investigator.py`；不新增通用插件注册框架。

物流T07新增 `logistics.py`：六个只读工具的函数声明、输入/输出结构和严格参数校验，`LogisticsTools(execute)`接收宿主注入执行函数。`backend/logistics_tools.py`按真实run绑定快照、实时授权并复用T04指标；不注册自由SQL。接口、分页、证据定位和阶段边界见 [T07说明](../../docs/logistics-execution/t07-tools.md)。

T08在物流宿主每次已启动工具执行前后保存调用审计与结果证据；响应增加call_id、result_evidence_id、result_fingerprint，get_evidence支持快照任务及LEV结果证据。核心声明同步来源类型及新增元数据，授权和SQLite仍由宿主负责；[证据/报告说明](../../docs/logistics-execution/t08-evidence-report.md)。
