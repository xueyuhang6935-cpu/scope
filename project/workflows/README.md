# 顺序、分支与回退

T12现已将物流固定一次修订替换为证据/工具不可用/截断/人工约束变化触发；select、execute和report可在约束变化时返回plan。宿主统一累计任务家族预算，规范化重复步骤复用证据，两个无进展观察停止，程序生成带未完成项的partial。旧T11操作日志/trace兼容恢复。详见[T12说明](../../docs/logistics-execution/t12-replanning-stops.md)；下段T11描述为接入时的历史边界。

物流T11图位于`logistics.py`：`context → plan → select → execute → observe → decide → report → finish`，选择和判断节点可回到工具或规划。核心接收宿主节点与检查点，不创建数据库、不访问模型配置。`backend/logistics_workflow.py`注入真实T07工具、T10模型及T08报告；稳定`thread_id=logistics:{run_id}`，每节点中断后以`durability='sync'`保存。复用T06单运行线程；检查点和节点操作日志配套恢复，未知inflight操作转waiting、不自动重发。当前只做一次观察后修订，完整T12策略及公开人工恢复未实现。详见[实施说明](../../docs/logistics-execution/t11-langgraph-loop.md)。

M5b 已迁入 `investigation.py`：`State`、五个节点、图连线、人工等待、同步检查点调用、重规划子调查调度、异常处理、启动恢复和单线程执行器关闭。

`InvestigationWorkflow(service, checkpointer)` 接收后端能力：阶段记账、取数、调查、方案保存、已提交审批收尾、运行状态读取、原子重规划、恢复列表和异常审计。核心不导入后端/FastAPI、不创建数据库连接、不读取环境配置。

后端 `investigation_service.py` 执行授权、快照校验及 PostgreSQL/SQLite 事务；`supply_workflow.py` 负责组装，保留原构造函数及工作流方法，打开原位置的 `supply-graph.sqlite`，先等待工作线程结束再关闭连接。API 位于 `investigation_routes.py`。

保持原节点 `collect → investigate → plans → approval → finish`；方案被阻断时直接结束。`thread_id` 仍为调查编号，持久化使用 `durability='sync'`。恢复审批节点前先建立缺失的 interrupt，恢复载荷不能代替已提交审批；模型请求前仍由后端提交 `model_attempted`，检查点重放不重复计费。

`replan` 的锁、请求编号幂等、权限与轮数校验及父子调查写入作为一个事务留在后端；工作流控制何时调用及如何执行子调查。仍只支持一个应用进程、一个图工作线程，未新增跨进程任务租约。
