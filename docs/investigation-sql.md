# Agent 只读 SQL 调查

M3 目录边界：`project/tools/investigation_sql.py` 定义工具声明、白名单、SQL 校验、结果与错误协议；`project/tools/executor.py` 执行调用保护并委托审计。后端 `investigation_sql.py` 注入专用受限连接，`investigation_audit.py` 保存 SQLite 审计，`investigation_harness.py` 保留工作流兼容入口。安装器仍在后端，权限计算仍由现有业务授权提供；表结构、工具返回字段和预算不变。

M4 判断循环已迁入 `project/agents/investigator.py`；工具分发与 Agent 侧的表字段读取门禁位于 `project/tools/investigation_dispatch.py`，SQL 工具自身的授权和门禁仍保留。模型 HTTP 请求由后端注入，Agent 核心不接收密钥。

调查仍由一个 Agent 和现有 LangGraph 编排。模型模式下，`get_database_schema` 提供授权表目录和字段；`execute_sql` 实际查询 PostgreSQL；模型可以根据结果或错误继续查询，最后通过 `submit_report` 引用 `sql:` 证据编号。

原来的三个无参数证据读取工具在模型查询模式下不再注册，避免继续返回固定证据摘录。`simulate_plan` 保留为读取确定性候选方案的工具，不接受模型方案参数；SQL 不取代缺料计算、方案试算或审批。

## 安装与启用

在 `Scope` 目录安装依赖，并安装查询角色和视图：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m backend.install_investigation_sql --local
```

`--local` 仅支持项目自带的 `127.0.0.1:55432/scope_analyst`，读取现有本地管理员配置，创建专用 `scope_agent_reader` 角色及 `agent_read` 视图，并向 `.env` 写入 `SUPPLY_AGENT_DATABASE_URL`，不输出口令。重复运行复用本地已配置的口令。

外部部署不使用 `--local`。管理员先配置 `ANALYST_DATABASE_URL`（应用账号）、`SUPPLY_AGENT_ADMIN_DATABASE_URL`（管理员连接）和 `SUPPLY_AGENT_READER_PASSWORD`，再运行安装模块；随后将专用角色的连接写入服务的 `SUPPLY_AGENT_DATABASE_URL`。管理员凭据仅用于安装，不应保留在运行服务中。先完成现有场景数据及 `sc` 风险表初始化。

启用真实模型还需要下列配置并重启后端；默认规则模式不会调用模型：

```dotenv
SUPPLY_INVESTIGATOR=model
SUPPLY_MODEL=your-tool-capable-model
SUPPLY_MODEL_BASE_URL=https://your-provider.example.com
SUPPLY_MODEL_API_KEY=your-key
SUPPLY_AGENT_MAX_ROUNDS=12
SUPPLY_AGENT_MAX_SECONDS=120
SUPPLY_AGENT_MAX_TOKENS=24000
```

预算上限分别为 24 轮、300 秒及 60000 个累计报告用量 Token。每次模型请求最多返回 1600 Token，单次查询超时 3 秒。累计 Token 在请求之间检查，单次请求可能越过剩余预算。未配置专用连接时返回错误，不回退到应用账号执行模型 SQL。

## 工具调用

```json
{"tables": ["inventory_snapshots", "inventory_reservations", "quality_holds"]}
```

以上为 `get_database_schema` 参数；省略 `tables` 返回授权目录，指定最多六张表返回完整列名及类型，避免每轮携带整个数据库结构。

```json
{
  "sql": "SELECT factory_id, SUM(quantity) AS quantity FROM inventory_snapshots WHERE material_id = %(material)s GROUP BY factory_id",
  "parameters": {"material": "P001"}
}
```

以上为 `execute_sql` 参数。允许单条 PostgreSQL SELECT、非递归 CTE、关联、聚合和限定的窗口/日期/文本函数。只接受工具目录中的无前缀表名或 `agent_read` 视图名，SQL 经 AST 校验和重新生成后才执行；具名参数交给数据库驱动绑定。结果列必须使用唯一名称；空结果的 `columns` 为空数组。

结果保存 SQL、参数、授权范围、查询时点、列名、行数据、截断标记及唯一证据编号。最多返回 100 行、约 16000 字节的行数据；超大单行也会在数据库端截断并从结果中省略。`truncated=true` 表示必须缩小范围继续查，不能把返回部分当成全量。小数以字符串保留精度。

查询失败返回脱敏错误，模型可修正列名、语法或筛选条件后再调用。`update_plan` 接受 `reason`（1至1000字）和 `steps`（1至5个步骤，每步1至500字），记录根据错误、证据不足或审批意见修订的调查计划。参数或报告引用不合法时也会返回可修正反馈，继续下一轮；同一工具和参数第三次出现时停止空转。所有调用共享原有轮次、时间和 Token 预算。权限或场景失效、网络失败或超时则停止并转人工。轨迹、计划版本及新证据进入调查报告和审计，最终报告只能引用实际返回给模型的证据编号。

## 统一调查授权与即时审计

所有模型工具调用通过统一调查检查，包括 `simulate_plan` 和 `submit_report`。执行前以及交付结果前重新核对当前成员、调查工厂权限、场景、回放时点和计算窗口。报告保存前在业务权限锁内再复核，并保持锁直到 PostgreSQL 提交；适用于当前单进程部署。

SQL 证据保留查询时的数据域／工厂范围，报告或下一次调用不能使用权限已缩小的旧证据。缓存证据可能具有间接所属关系，因此保守要求本次调查全部工厂的对应数据域权限；候选方案另检查目标及调出工厂的库存、采购、生产、质量和规则权限。校验失败转人工，不继续返回缓存方案，不交付报告。

工具调用即时写入业务 SQLite 的 `investigation_tool_calls`，与模型函数和 LangGraph 节点返回分离。调用前提交编号、调查编号、工具和参数，并在授权后、执行前保存范围；结束后立即提交证据结果、截断信息、错误及耗时。状态为 `started`、`completed`、`failed` 或 `interrupted`。授权失败的调用可能没有授权范围；不会返回未经授权的结果。

重启恢复和流程异常将尚未完成的调用标为 `interrupted`，已完成记录保持原样，不自动续跑或重放模型收费请求。备份需包含业务 SQLite。审计查询接口 `GET /api/supply/investigations/{id}/tool-calls` 要求当前调查工厂查看权限，并检查记录中涉及的数据域权限；接口返回已保存的参数、范围和结果。

## 权限与数据口径

- 专用角色不拥有业务表，不授予源表权限；默认只读事务。安全屏障视图负责行范围，AST 白名单限制对象、函数及语句结构。
- 每次工具调用按请求人的当前成员资格、工作空间、场景、数据域与工厂重新授权。不同角色的工厂及数据域不会交叉拼接。
- 视图的上下文由服务端在事务内设置，模型无法调用 `set_config`、`SET`、目录表、任意函数、写操作或多语句。数据库连接凭据属于可信后端，不向模型提供；视图不是供不可信客户端持凭据直接连接的接口。
- 所有源记录满足场景匹配及 `ingested_at <= asOf`。供应承诺还要求 `known_at`、`changed_at` 不晚于该时点。间接所属工厂通过库存、采购、发运等外键追溯；调拨必须两端均授权。
- `materials` 等参考主数据限定当前场景；采购、库存、质量、生产和规则数据分别按对应数据域筛选。
- `commitment_changes` 只显示当前工作空间、当前场景、已获知且采购行所属工厂已授权的变更。它是补充历史，查询最新承诺时须与 `supplier_commitments` 结合，不能把原始承诺视为最终状态。
- 业务未来时点可能合法，例如已知的下周到货承诺。不要把 `available_at` 当成实际收货时间。SQL 查询结果不自动证明因果关系。
- 返回库存数量不等于可用库存；需要考虑快照、预留、冻结、单位、物料版本、有效 BOM 和需求窗口。最终缺料与方案可行性继续以已有确定性工具结果为准。

## 验证

```powershell
$env:SCOPE_DATABASE_INTEGRATION='1'
.\.venv\Scripts\python.exe -m unittest project.tests.integration.test_investigation_sql project.tests.integration.test_supply_workflow project.tests.integration.test_agent_loop backend.test_shortage -v
npm run lint
```

模型循环测试使用模拟 HTTP 响应，不消耗外部模型调用。数据库测试使用真实本地 PostgreSQL；工作空间隔离测试仅插入并清理自身随机标识的测试记录。

统一授权和中断持久化检查：`.\.venv\Scripts\python.exe -m unittest project.tests.integration.test_investigation_harness -v`。工作流测试同时验证报告落库前撤销数据域权限以及审计接口读取。
