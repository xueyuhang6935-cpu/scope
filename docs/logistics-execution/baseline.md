# T01：现状基线与可回退开发点

核查日期：2026-10-07（北京时间）。项目目录：`E:\数据异常调查\Scope`。

任务状态：`done`。已定位真实代码入口、验证隔离启动、建立源码回退点并记录现有失败；不表示旧系统所有回归通过。

依据：工作区上级目录《上海物流揽收时效风险调查Agent_执行方案_V1.md》T01。此次只建立现状基线，不实施 T02 数据契约或物流业务功能。实际代码优先于 README 的历史描述。

## 1. 开发起点与回退

- `git status --short`、`git branch --show-current`、`git rev-parse HEAD` 均返回 `not a git repository`。当前目录及父目录没有可用 Git 仓库，不能提供提交号或建立功能分支；未擅自初始化仓库。
- 检查 `E:\AGENTS.md`、工作区及 Scope 根目录，并搜索项目代码目录，未发现适用的 `AGENTS.md`。
- 开发点标识：`T01-20261007-150009`，以源码归档及逐文件指纹代替 Git 提交。已有文件保留，本次不修改业务源码、依赖、配置和数据库源表。
- [源码归档](../../test-results/logistics-t01/20261007-150009/source-baseline.zip)包含 173 个文件，覆盖根目录工程文件（排除实际 `.env`）、backend、src、scripts、server、tests、docs、plan、public、assets。
- [逐文件清单](../../test-results/logistics-t01/20261007-150009/source-manifest.json)记录路径、字节数及 SHA-256。归档 SHA-256：`b0c805da70209fa7e3792fe6c95ca66e26921660a0d5fd0d90310c5bad7f57b0`。
- 已将归档解压到独立临时目录，173 个文件的哈希全部匹配：[恢复校验](../../test-results/logistics-t01/20261007-150009/restore-check.json)。这证明源码可还原，不等于完整应用灾备验证。

归档不包含 `.env`、运行数据库、依赖目录、构建产物及测试产物。它不是数据库备份；后续数据库变更仍须建立一致性备份和迁移方案。不要用此归档覆盖运行中的数据库或用户后续修改。

需要回退代码时，先保存当前改动，再将归档解压到新的空目录，比对清单后选择性恢复。本次还原核验使用 Python 标准库 `zipfile` 和 `hashlib`，没有对原目录执行清空或覆盖。

## 2. 环境、依赖与存储

| 项目 | 实际核查结果 |
| --- | --- |
| Node.js / Python | v24.19.0 / 3.11.9；复用 `.venv/Scripts/python.exe` |
| 前端锁定版本 | React 19.3.0、TypeScript 5.8.3、Vite 6.4.3、Playwright 1.63.0；`package-lock.json` lockfileVersion=3 |
| 后端已安装版本 | FastAPI 0.141.1、Uvicorn 0.54.0、LangGraph 1.2.12、langgraph-checkpoint-sqlite 3.1.1、psycopg 3.3.6、sqlglot 27.29.0 |
| Python 锁定一致性 | 逐项比较 `requirements.lock.txt` 与已安装包版本，没有发现不一致；未重新安装或升级依赖 |
| 认证存储 | `AUTH_DB_PATH`，当前解析为 `data/auth.sqlite` |
| 本地业务存储 | `DATA_DB_PATH`，当前解析为 `data/supply.sqlite`；导入、治理、会话、权限、工具审计等 |
| LangGraph 检查点 | `backend/supply_workflow.py` 从业务 SQLite 路径派生同目录 `supply-graph.sqlite`，当前为 `data/supply-graph.sqlite` |
| PostgreSQL 应用连接 | `ANALYST_DATABASE_URL`；只读连接成功，存在 `lade`、`supply_demo`、`sc`、`agent_read` |
| PostgreSQL Agent 连接 | `SUPPLY_AGENT_DATABASE_URL`；连接成功，能查询 `agent_read.inventory_snapshots`，无上下文时返回 0 行；完整权限审计尚未执行 |
| 业务数据现状 | `supply_demo.scenarios` 实查 0 行；`lade.import_batches` 中 `verified_at IS NOT NULL` 的批次实查 1 个。这里只核查运行前提，未核对任务全量、19 字段或时间质量 |

配置加载由 `backend/main.py:create_app` 合并 `.env`、进程环境变量及调用参数；调用参数优先。`backend/__main__.py` 读取监听地址和端口。仅保存配置名，不输出连接字符串、密码或密钥。脱敏环境证据见 [environment.json](../../test-results/logistics-t01/20261007-150009/environment.json)。

需要认识的配置名：`AUTH_HOST`、`AUTH_PORT`、`AUTH_DB_PATH`、`DATA_DB_PATH`、`SCOPE_PYTHON`、`SCOPE_API_PROXY_TARGET`、`ANALYST_DATABASE_URL`、`SUPPLY_AGENT_DATABASE_URL`、`SUPPLY_MONITOR_AUTOSTART`、`SUPPLY_PLANNER`、`SUPPLY_INVESTIGATOR`、`SUPPLY_MODEL`、`SUPPLY_MODEL_BASE_URL`、`SUPPLY_MODEL_API_KEY`、`SUPPLY_AGENT_MAX_ROUNDS`、`SUPPLY_AGENT_MAX_SECONDS`、`SUPPLY_AGENT_MAX_TOKENS`、`SUPPLY_MAX_REPLANS`。认证渠道另使用 SMTP、Twilio、微信等配置。

## 3. 实际入口与启动方法

以下命令均从 Scope 目录执行，现有依赖已安装：

```powershell
Set-Location 'E:\数据异常调查\Scope'
# 开发：后端与 Vite 一起运行。
npm run dev
# 或构建后由后端托管页面。
npm run build
npm start
```

两种启动方式任选其一。`npm run dev` 使用 concurrently，后端命令为 `node scripts/start-backend.mjs --reload`，前端为 `vite --port=3003 --host=0.0.0.0`。`npm start` 和 `scripts/start-local.ps1` 都走 `node scripts/start-backend.mjs` → 项目 Python → `python -m backend` → `backend.main:create_app`。

| 地址或端口 | 用途 |
| --- | --- |
| `http://localhost:3003` | 开发前端；Vite 将 `/api` 代理至 `SCOPE_API_PROXY_TARGET`，默认 3002 |
| `http://localhost:3002/api/health` | 默认后端健康接口，只执行认证 SQLite `SELECT 1`，不是 PostgreSQL/模型 readiness |
| `http://localhost:3002/docs` | FastAPI 文档 |
| `http://localhost:3002` | 构建后的页面，依赖 `dist/index.html` |
| 3010 / 3012 | Playwright 测试前端 / `supply-live.spec.ts` 的测试后端 |

启动实测复用了正式 Node 启动入口，使用临时 SQLite、随机空闲本机端口、关闭监测及模型，避免触发现有工作空间恢复或模型请求。健康接口、`/docs`、`/openapi.json` 和 `/` 均返回 200；随后停止本次进程树并删除临时目录。见 [启动结果](../../test-results/logistics-t01/20261007-150009/startup-entrypoint.json)及[启动日志](../../test-results/logistics-t01/20261007-150009/startup-entrypoint.log)。因此已证明入口可启动，但不宣称原业务库上的审批全链路已通过。

初次直接启动 Python 的辅助检查同样获得四个成功响应，但只终止虚拟环境启动进程导致临时 SQLite 被子进程占用，清理报 WinError 32。之后已停止该测试端口的子进程，并改用进程树清理完成上述复测。这是本次检查脚本的清理问题，不计为产品启动失败。初次日志保留在同一证据目录的 `startup.log`、`startup.json`。

## 4. 已有能力及后续任务定位

| 能力 | 现有真实实现 | 复用 / 新增 / 待核对及任务 |
| --- | --- | --- |
| HTTP、认证、错误与页面托管 | `backend/main.py`、`backend/auth.py`、`backend/common.py` | 复用；物流路由尚待新增（T03 起） |
| 数据存储与导入 | `backend/data_store.py`、`backend/data_import.py`、`backend/data_access.py` | 复用数据访问和授权模式；新增物流 Repository 与快照（T02—T03） |
| LaDe 接入 | `scripts/import-lade.py`、`backend/database_connection.py`、`docs/lade-database.md`、`src/supply/DatabaseConnection.tsx` | 已支持批次发现、保存和预览；T02 核对实际字段、批次、主键及时间，不重新导入 |
| 数据预览边界 | `/api/data/database`、`/test`、`/preview`，`database_source_connections` 保存批次 | 当前是数据管理预览，暴露完整历史任务字段，不能直接充当 T 时刻快照；T03—T05 新建受控快照边界 |
| 工作空间与权限 | `backend/business_access.py`、`backend/data_access.py` | 复用成员、角色、审计；现为工厂/数据域/额度，不等同物流城市与区域权限（T02、T03、T17） |
| 聊天任务 | `backend/supply.py`、`backend/supply_context.py`、`src/supply/Assistant.tsx`、`src/supply/AnalysisMessages.tsx` | 已有 SQLite 会话任务、2-worker 线程池及近期上下文；物流结构化状态与 ContextBuilder 待做（T06、T14） |
| 风险计算 | `backend/shortage.py`、`backend/risk_monitor.py`、`backend/risk_schema.sql`、`src/supply/RiskWorkspace.tsx` | 现为供应链缺料逻辑；物流三指标、时间边界和风险规则需新增（T04—T05） |
| 调查启动 | `backend/supply_workflow.py:routes/start_investigation/process`；`POST /api/supply/risks/{event_id}/investigate` | 真后端入口已定位；物流 run、领取凭证、任务家族预算和恢复契约需补齐（T06、T12） |
| LangGraph | `backend/supply_workflow.py` 的 `collect → investigate → plans → approval → finish`，SqliteSaver、interrupt、Command(resume)、recover | 复用持久化和中断；当前单进程图执行器 max_workers=1，不能直接增加应用 worker；物流证据驱动调查图待做（T11、T18） |
| 调查与模型 | `backend/investigation_agent.py`、`backend/supply_model.py`、`backend/prompts/` | 已有计划、工具循环和有限预算；物流计划依赖、修订依据、累计预算须独立核对和扩展（T10—T13） |
| 工具、证据与报告 | `backend/investigation_sql.py`、`backend/investigation_harness.py`、`backend/investigation_report.py`、`src/supply/InvestigationReport.tsx` | 复用授权、审计和引用协议；现有 SQL 表范围是供应链。物流使用参数化工具、快照证据、模板报告及受控 Markdown 导出（T07—T08） |
| 审批 | `backend/supply_planning.py`、`backend/supply_workflow.py`、`backend/workflow_schema.sql`、`src/supply/Workflows.tsx` | 有方案摘要/快照/权限/自批限制、驳回与恢复代码；数据库场景为空，真实端到端本次未验证；物流版本审核、反馈和恢复一致性待做（T16—T18） |
| 运行页面 | `src/supply/SupplyPages.tsx:Runs` 从 `src/supply/demo.ts` 读取示例 | `/runs` 尚非真实运行列表；真实调查通过风险详情、调查接口和工具审计查看。T09 新增持久化事件流并接真实列表 |
| 处置与复核 | 现有业务流程止于审批 | T19—T20 新增内部跟进单、回执及新快照复核，不能把批准当外部派单成功 |
| 评测 | `backend/evals/supply_agent_cases.json`、`backend/eval_supply_agent.py`、`backend/test_*.py`、`tests/*.spec.ts` | 复用通用案例，T02 先设计物流评测，T21—T22 完成冻结案例和对照；离线不证明真实模型质量 |
| 备份 | `scripts/backup-storage.mjs`、`restore-storage.mjs`、`verify-backup.mjs` | 历史 PostgreSQL/S3 工具，不覆盖当前应用全部 SQLite；T23 才做完整一致性备份与恢复演练 |

## 5. 实际验证

全部证据位于 `test-results/logistics-t01/20261007-150009/`。测试命令直接调用现有入口，未增加测试框架或重写测试。

| 检查 | 实际命令或方式 | 结果 | 证据 |
| --- | --- | --- | --- |
| 类型检查 | `npm run lint`（实际为 `tsc --noEmit`） | 通过，退出码 0 | `lint.log` |
| 生产构建 | `npm run build` | 通过，退出码 0；主 JS 583.77 kB，存在超过 500 kB 的既有构建警告 | `build.log` |
| 后端回归 | 清除 `SCOPE_DATABASE_INTEGRATION` 后执行 `.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s backend -t . -p 'test_*.py' -v` | 共 104 项：73 通过、31 跳过，0 失败，退出码 0 | `backend-offline.log` |
| Agent 离线评测 | `.\.venv\Scripts\python.exe -X utf8 -m backend.eval_supply_agent --output test-results/logistics-t01/20261007-150009/agent-eval-offline.json` | 24/24 协议通过；使用模拟响应，没有真实模型质量结论 | `agent-eval.log`、`agent-eval-offline.json` |
| 启动冒烟 | 正式 Node 入口 + 临时 SQLite / 测试环境 | 四个 HTTP 路径 200，进程树清理成功 | `startup-entrypoint.json`、`startup-entrypoint.log` |
| 数据库前提 | psycopg 只读事务，5 秒语句超时，统计场景与已核验批次 | 应用及 Agent 连接可用，供应链场景 0，LaDe 批次 1 | `environment.json` |
| 代码回退点 | ZIP 校验、独立目录解压、逐文件 SHA-256 比对 | 173/173 一致 | `source-manifest.json`、`restore-check.json` |
| 浏览器回归 | 下方七个现有 spec 文件，模拟接口测试 | 33 项：24 通过、9 失败，耗时 6.8 分钟，Playwright 退出码 1；LaDe 接入用例通过 | `playwright.log`、`playwright/*/error-context.md` |

浏览器回归失败详情见下节。实际命令：

```powershell
npx playwright test tests/auth-design.spec.ts tests/assistant.spec.ts tests/data-connections.spec.ts tests/supply.spec.ts tests/workflows.spec.ts tests/investigation-report.spec.ts tests/lade-connection.spec.ts --output test-results/logistics-t01/20261007-150009/playwright
```

明确未运行：启用数据库开关的完整后端集成测试、`tests/supply-live.spec.ts`、真实模型 `--live` 评测、生产账号登录/审批、数据库灾备、LaDe 全量导入复核。数据库集成缺少旧场景数据；`test_real_database_save_isolation_preview_and_restart` 还假定默认连接返回旧 45 表/12 场景，与目前 LaDe 优先发现逻辑不兼容。应在隔离供应链测试库重建相应夹具并区分数据源后运行，不能为基线向当前源库重新灌入旧模拟数据。

执行过程曾因 PowerShell 默认编码误读含中文的日志路径，导致最初日志重定向失败，检查未启动；改为显式 UTF-8 后执行了上表真实检查。PowerShell 将构建 stderr 包装为 NativeCommandError，但真实构建退出码为 0，应按实际退出码与日志判断。

## 6. 已有失败与限制

浏览器测试均在改动业务代码之前的相同源码上执行；[源码对比](../../test-results/logistics-t01/20261007-150009/source-change-check.json)确认归档的 173 个既有文件未改变。这里记录的是现状失败，不把尚未验证的原因当作最终根因。

| 用例位置 | 本次观察 | 后续排查方向 |
| --- | --- | --- |
| `tests/data-connections.spec.ts:4` | 期望两处“未配置”，实际一处 | 用例还假定数据库配置服务未开放；补齐数据库 API 模拟并对齐当前 LaDe/供应链连接界面 |
| `tests/data-connections.spec.ts:55`、`:100` | 等待“预览文件”超时，按钮带“当前角色或范围没有此操作权限”且 disabled | 用例将 `/api/supply/**` 返回 503，没有提供有效 `/access/me` 角色；先修测试权限夹具，不放宽产品鉴权 |
| `tests/workflows.spec.ts:10` | 等待“预览文件”超时，记录按钮禁用及 DOM 被替换 | 此文件只统一模拟认证与数据接口，缺少完整业务权限模拟；检查权限加载与导航 |
| `tests/workflows.spec.ts:47` | 等待“负责人”控件超时 | 核对跳转后的页面、权限响应和当前表单契约 |
| `tests/workflows.spec.ts:78` | 等待“记录模拟确认任务并继续”点击超时，DOM 被替换 | 核对旧模拟流程与权限轮询造成的页面变化 |
| `tests/workflows.spec.ts:118` | 等待“已核对 APR-001”超时 | 对齐当前审批模拟界面与接口模拟，不据此认定真实审批后端失败 |
| `tests/workflows.spec.ts:129` | 模拟审批流程超时 | 以对应 error-context 的控件和页面快照继续排查 |
| `tests/workflows.spec.ts:144` | 填写“物料负责人”时 DOM 被替换，等待超时 | 核查当前治理页面、权限加载与旧模拟用例的契约 |

原始断言、调用日志及页面快照保存在 `playwright/` 各失败目录的 `error-context.md`；完整退出结果保存于 `playwright.log`。这些文件是测试输出，不能替代业务验收。

初次 Agent 数据库探测误把对未授权 `supply_demo` 对象的元数据查询失败当作连接失败；已分离连接与对象权限，用 `SELECT 1`、只读状态及授权 `agent_read` 视图查询确认连接正常，`environment.json` 保存更正结果。没有修改数据库角色或权限。

## 7. 本次变更和后续入口

新增实施文档只有 `docs/logistics-execution/baseline.md` 和 `task-status.md`；其余新增文件为 `test-results/logistics-t01/20261007-150009/` 中的基线归档、证据与日志。既有测试可能按自身定义刷新 `test-results/` 中截图，生产构建刷新 `dist/`；它们均为生成产物。未修改业务实现或实际配置，因此不新增代码变更记录。

T01 的完成条件是基线可追踪、能按入口启动、既有失败和未运行项已明确，并非所有历史测试必须全绿。下一步为 T02：先核对 LaDe 源数据与业务任务键、字段白名单、参考时间和权限，再建立 `data-contract.md`、`snapshot-contract.md`、`eval-design.md`。本次不把源表物理主键 `(batch_id, source_row)` 直接等同于方案业务任务键 `(dataset, city, order_id)`，具体映射留给 T02 验证。
