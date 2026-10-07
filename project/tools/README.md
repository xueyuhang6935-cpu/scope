# 独立外部能力

M3 已迁移：

- `investigation_sql.py`：两个 SQL 工具声明、表与函数白名单、SQL 校验、结构读取门禁、结果限量与证据封装、错误协议。`InvestigationSQL(connect, authorize)` 每次调用重新授权，使用 `connect(scope)` 提供的上下文管理器执行查询。
- `executor.py`：`InvestigationHarness(store, authorize)` 负责已读证据和候选方案的权限复核、调用编号、耗时及审计生命周期委托。

后端 `investigation_sql.py` 保留旧构造接口并提供受限连接：专用 DSN、只读事务、超时、角色校验、授权上下文；驱动错误仅以 `DatabaseQueryError(sqlstate)` 传回工具。`investigation_audit.py` 负责 SQLite 审计表与读写，`investigation_harness.py` 负责组装及重启中断入口。无第二份工具实现。

本目录不读取环境配置、不创建数据库连接、不导入 backend/FastAPI。授权事实由后端实时计算，工具只执行现有保护规则。Agent 决策循环、预算与工具分发留待 M4；不新增通用插件注册框架。
