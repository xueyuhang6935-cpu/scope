# SupplyPilot 供应链智能助手

> 实现核对日期：2026-10-06（北京时间）。本说明依据 [代码修改记录](plan/) 与当前代码整理，最新功能记录为 [20261006-185430](plan/20261006-185430.md)。

Scope 是 SupplyPilot 的 Web 应用，围绕汽车供应链的库存、缺料风险、调查取证、处置方案试算和人工审批提供工作空间。前端使用 React + TypeScript + Vite，后端使用 Python FastAPI，调查流程由 LangGraph 编排。

当前已接通“数据接入 → 缺料扫描 → 风险调查 → 调拨／加急方案试算 → 人工审批 → 编排恢复／重新规划”。文件导入处理用户上传的数据；数据库计算使用 PostgreSQL 中的人造供应链场景。**业务流程止于审批决定，尚未接入 ERP/WMS 执行、库存预占、收货回写和正式风险关闭。**

## 当前功能

调查 API 定义在 `backend/investigation_routes.py`，权限、审批事务和调查读写由 `backend/investigation_service.py` 提供；图与恢复控制位于 `project/workflows/investigation.py`，`backend/supply_workflow.py` 组装业务服务和原 SQLite 检查点，接口路径保持兼容。

| 模块 | 已实现能力 | 使用条件与边界 |
| --- | --- | --- |
| 账号与登录 | 注册、登录、验证码、找回密码、微信登录、会话保持 | 邮件、短信及正式微信登录需要对应服务配置；账号设置中的部分操作尚未接入 |
| 数据接入与治理 | CSV/XLSX 上传、字段映射、质量校验、批量提交、原文件下载、单位与物料映射、复检和变更记录 | 单文件最多 10 MB、10,000 条记录；文件数据不会自动变成完整的缺料计算模型 |
| 智能助手 | 库存查询、质量检查、缺料计算、上下文追问、聊天内调查／方案／审批入口、持久化会话与证据 | 明确且受支持的只读问题直接执行；范围冲突、条件缺失或不支持时追问；缺料计算需要数据库场景 |
| 工作台与风险处置 | 自动／手动扫描、事件合并、负责人管理、风险确认、交期变更、证据及版本历史 | 从 `supply_demo` 计算，运行结果写入 `sc`；“当前窗口已覆盖”不等于业务正式关闭 |
| 调查与方案 | 确定性调查、调拨／加急／组合试算、可选模型 SQL 取证、计划修订、结构化报告、逐条证据展开与工具审计 | 数量、费用、可行性由程序计算；报告区分直接观察、外部陈述与推断，引用有效不代表结论已被证明 |
| 审批中心 | 方案版本与摘要核对、跨账号审批、额度检查、暂停与恢复、驳回／失效后自动重规划 | 发起人不能自批；新方案仍须人工审批；批准不会自动下发业务单据 |
| 角色与权限 | 工作空间、多角色、工厂／数据域范围、审批额度、成员停用、审批规则与审计 | 后端执行授权校验；系统管理员不自动获得业务审批权 |
| 提示词与评测 | 范围解析／调查提示词版本追踪、24 个固定评测案例、离线协议检查、显式真实模型评测与人工语义审核 | 离线通过只说明流程和协议符合预期；真实模型质量须另行评测及审核 |
| 示例页面 | 固定风险、审批、导入及运行轨迹演示 | `EVT-*`、`APR-*`、`RUN-*` 等固定对象仍为样例；真实风险和方案使用 `RISK-*`、`PLAN-*` |

登录后默认进入 `/workspace`。主要入口为 `/assistant`、`/risks`、`/approvals`、`/data/connections`、`/data/catalog`、`/data/governance`、`/runs`、`/settings` 和 `/account`。`/runs` 当前仍是示例运行列表，真实调查轨迹在风险详情的调查面板查看。

助手支持直接衔接调查和方案：问“这个缺口怎么算的”解释原结果并显示依据时间；问“现在还是缺吗”创建新计算；问“继续查为什么晚交”或“哪个方案更好”关联原计算中的风险并启动／复用调查试算。多风险先选择具体工厂、物料和版本。聊天显示后台真实阶段、报告和候选方案，可展开方案核对并通过现有审批接口提交人工决定。自然语言“批准”仅打开核对入口，不会自动批准或执行业务单据。详见 [聊天动作与调查衔接](docs/chat-actions.md)。

会话上下文最多保留六个近期任务摘要，优先保留当前聚焦的工厂、物料和版本，并携带已有观察、未解问题与方案驳回原因。解释使用已保存结果，不会重新查询；刷新会创建新计算，但不代表上游数据已实时同步。模型调用前后复核所引用报告的权限，完整证据仍需按需读取。

## 快速启动

以下命令在 Scope 目录的 PowerShell 中执行。推荐 Node.js 24+、Python 3.11+；项目已有依赖和虚拟环境时可复用。

```powershell
Set-Location 'E:\数据异常调查\Scope'
npm ci
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt

# 首次配置时创建；已有 .env 不覆盖。
if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
}
```

按需修改 `.env` 后启动：

```powershell
npm run dev
```

| 地址 | 用途 |
| --- | --- |
| `http://localhost:3003` | Vite 开发前端，代理 `/api` 到后端 |
| `http://localhost:3002/api/health` | 后端健康检查，仅检查认证 SQLite，不代表 PostgreSQL 或模型服务已就绪 |
| `http://localhost:3002/docs` | FastAPI 接口文档 |

基础启动、已有账号登录、文件导入及库存／质量查询不需要 PostgreSQL、S3 或模型密钥。`.env.example` 中的数据库地址、邮件地址和密钥均为占位值，不能直接用于真实服务。

首次使用没有账号时，注册及找回密码需要配置邮件或短信验证码服务。本机体验也可在 `.env` 设置 `WECHAT_MOCK_LOGIN=true`，重启后通过页面微信登录入口使用本地测试账号；此模式仅允许本机访问，且在 `NODE_ENV=production` 时禁用。

构建后可由 FastAPI 同时提供页面和 API：

```powershell
npm run build
npm start
```

此时访问 `http://localhost:3002`。`npm start` 和 `scripts/start-local.ps1` 只启动后端，不自动启动 PostgreSQL/S3。没有 `dist/index.html` 时，后端页面入口会提示先构建。

## 配置说明

配置模板见 [.env.example](.env.example)。后端读取 `.env` 及进程环境变量，更改配置后需重启；前端构建变量变更后需重新启动 Vite 或构建。

| 配置 | 用途／默认行为 |
| --- | --- |
| `AUTH_HOST`、`AUTH_PORT` | 后端监听地址与端口，默认 `0.0.0.0:3002` |
| `AUTH_DB_PATH` | 账号及会话 SQLite，默认 `data/auth.sqlite` |
| `DATA_DB_PATH` | 导入、治理、助手任务、权限及本地审计 SQLite，默认 `data/supply.sqlite` |
| `SCOPE_PYTHON` | 启动脚本使用的 Python；默认优先项目 `.venv`，否则使用 `python` |
| `SCOPE_API_PROXY_TARGET` | Vite 开发代理目标，默认 `http://localhost:3002`；在启动进程的环境变量中设置 |
| `VITE_AUTH_API_BASE_URL`、`FRONTEND_ORIGIN` | 前后端分开部署时的 API 地址与允许的前端来源 |
| `AUTH_CROSS_SITE_COOKIE` | 跨站 Cookie 配置；跨站部署使用 HTTPS |
| `SMTP_URL`、`SMTP_FROM`、`SMTP_PASSWORD` | 邮件验证码；密码可单独配置 |
| `TWILIO_ACCOUNT_SID`、`TWILIO_AUTH_TOKEN`、`TWILIO_FROM` | 短信验证码 |
| `WECHAT_APP_ID`、`WECHAT_APP_SECRET`、`WECHAT_REDIRECT_URI` | 正式微信网站登录 |
| `ANALYST_DATABASE_URL` | 当前供应链场景使用的 PostgreSQL 连接；沿用历史变量名 |
| `SUPPLY_MONITOR_AUTOSTART` | 默认启用自动监测，设为 `0` 禁用后台扫描线程 |
| `SUPPLY_PLANNER` | 助手问题解析；默认规则模式，设为 `model` 启用可选模型解析 |
| `SUPPLY_INVESTIGATOR` | 风险调查；默认 `rules`，设为 `model` 启用模型工具调用 |
| `SUPPLY_MODEL`、`SUPPLY_MODEL_BASE_URL`、`SUPPLY_MODEL_API_KEY` | 两类模型能力共用的模型名称、HTTPS 地址及服务端密钥；密钥兼容 `DEEPSEEK_API_KEY` |
| `SUPPLY_AGENT_DATABASE_URL` | 模型 SQL 专用只读连接，不使用应用写入账号替代 |
| `SUPPLY_AGENT_MAX_ROUNDS`、`SUPPLY_AGENT_MAX_SECONDS`、`SUPPLY_AGENT_MAX_TOKENS` | SQL 调查每轮默认预算：12 次模型调用、120 秒、24,000 个累计已报告 Token |
| `SUPPLY_MAX_REPLANS` | 自动重规划默认最多 3 轮，不含首次调查；`0` 关闭，最大 `5` |

例如后端改为 3004 端口时，在启动终端设置：

```powershell
$env:AUTH_PORT = '3004'
$env:SCOPE_API_PROXY_TARGET = 'http://localhost:3004'
npm run dev
```

模型密钥只在服务端配置，不使用 `VITE_` 前缀。规则模式可完成缺料计算、调查试算和人工审批；模型解析与模型调查是两个独立开关。模型调用结果或用量未知时不自动重放请求。

范围解析与调查提示词分别位于 `project/prompts/scope_parser.txt` 和 `project/prompts/investigator.txt`。提示词版本含内容摘要，写入解析元数据、调查报告和工具审计；修改提示词后需重启后端。调查预算按每轮关联调查计算，单次模型调用的耗时或供应商用量可能跨越本地阈值，不能将其视为供应商计费硬上限。

## 接入供应链场景

1. 准备 PostgreSQL，在 `.env` 设置 `ANALYST_DATABASE_URL`。日常应用账号需要读取 `supply_demo`，并能够创建和读写 `sc`；首次生成模拟数据还需要创建 `supply_demo` 的权限。
2. 生成或校验场景数据：

   ```powershell
   node scripts/seed-supply-demo.mjs
   ```

   脚本创建 12 个模拟场景、45 张表、4,362 条记录。同版本重复运行逐值核对，不重复插入；发现不同版本或内容时停止，避免覆盖。数据说明和校验结果在 [data/supply-demo](data/supply-demo/)。
3. 登录后在“数据中心 → 数据接入 → 配置数据库”测试连接、选择场景并保存接入。
4. 进入风险处置页，将计算窗口设为 3 天；在工作台或风险页点击“立即扫描”。默认自动扫描间隔为 60 秒，可按工作空间配置。

已有本机存储安装可用 `npm run storage:start` 启动原 PostgreSQL 与 SeaweedFS。此脚本依赖现有 Windows 二进制和 `data/services` 配置，会同时启动历史 S3 服务，不是通用 PostgreSQL 安装命令；当前供应链功能本身不依赖 S3。

| 场景 | 三天窗口的核对示例 |
| --- | --- |
| `normal` | 正常供应，无实际缺料 |
| `supplier_delay` | A 厂 P001 最大缺口 400 件，首次缺料为北京时间 2026-10-07 16:00 |
| `partial_transfer` | 缺口 400 件；B 厂保守可调拨 250 件，加急 150 件；组合费用 3,500 元，试算剩余缺口为 0 |
| `quality_hold` | 800 件账面库存中 300 件冻结，对 600 件需求缺 100 件 |
| `missing_unit` | 缺少单位换算时显示不可计算，不将缺失数据当作零 |

场景窗口固定为 2026-10-05 至 2026-10-18（北京时间）。`days` 表示从场景起点起计算的 1–14 天，`asOf` 表示已获知证据的截止时点，不是需求窗口起点，也不会随系统时钟自动前移。更改交期、回放时点或窗口后，结果可能与上表不同。

文件导入只提供物料、数量、单位等记录，不能替代 BOM、生产计划、采购承诺、物流和质量放行等完整数据。数据库计算不会读取 `expected_results` 或预置审批／关闭样例作为答案。

## 调查、审批与模型取证

在真实风险详情点击“调查并试算”，查看事实、证据和候选方案。流程为 `collect → investigate → plans → approval（等待人工）→ finish`，运行位置由 LangGraph 检查点保存。

首次创建工作空间时，创建者获得系统管理员、数据管理员和生产计划员角色，不包含审批人。由管理员在“系统设置 → 角色与权限”向另一位已注册账号授予审批人角色，配置相关工厂和额度，再由该账号在审批中心核对并批准或驳回。跨厂调拨需要覆盖来源和目标工厂；系统管理员不能给自己增加审批授权。

批准后恢复编排并记录决定；驳回或发现方案失效时，在原授权范围内自动发起关联调查。相同快照下已驳回方案不会再次提交；没有其他可行方案、权限失效或达到轮次上限时转人工。新一轮仍须人工审批。

模型调查使用单 Agent 按顺序调用工具：必须先创建调查计划，执行 SQL 前读取所涉及表的字段；可根据工具错误修正查询、修订计划，并在报告中引用实际读取的证据。接近时间、Token、上下文预算或到达最后一轮时，只允许提交报告；硬预算耗尽、网络失败或重复空转时保留可交付的部分结果并转人工，不自动重试模型请求。

`get_candidate_plans` 只读取后端预计算方案，不支持模型任意参数试算；`get_evidence` 只读取当前调查或显式关联的父调查证据，并复核权限、工作空间、风险事件及完整快照。不同快照或缺少快照指纹的旧报告不能作为当前调查证据。当前未引入多 Agent 协作或并行查询。

启用模型 SQL 调查前先完成场景和 `sc` 初始化，再安装专用只读角色及视图：

```powershell
# 仅用于项目现有本机 127.0.0.1:55432/scope_analyst 安装。
# 创建 scope_agent_reader / agent_read，并写入 .env 的专用连接。
.\.venv\Scripts\python.exe -m backend.install_investigation_sql --local
```

随后配置 `SUPPLY_INVESTIGATOR=model` 及模型名称、地址、密钥并重启后端。外部 PostgreSQL 的安装方法、SQL 限制和预算说明见 [Agent 只读 SQL 调查](docs/investigation-sql.md)。未配置专用只读连接时，不会回退到应用账号执行模型 SQL。

### 调查报告与证据核对

风险详情、聊天调查和审批入口复用同一报告组件，显示确定性计算结果、调查计划修订、工具轨迹及可展开的原始证据。新模型报告逐条提交结论，分为以下三类：

| 结论类型 | 报告含义与核对要求 |
| --- | --- |
| `observed`：直接观察 | 模型从已读取数据中归纳的观察，仍须核对证据；不等同于程序计算结果 |
| `reported`：外部陈述 | 供应商等外部主体的说法，必须说明局限，尚未独立验证 |
| `inferred`：推断 | 根据证据提出的解释，必须说明局限，不能当作已确认根因 |

每条结论必须引用实际读取的证据。截断结果在报告中明确提示，引用该结果的结论必须说明局限，不能据此推断全部数据。报告同时列出未解决问题和下一步建议；建议不表示已执行。

报告状态为 `complete` 或 `partial`：完成报告须有有效结论且无未解问题；部分报告须说明未完成事项。**有效的部分报告不会仅因调查未完成而否定后端已计算的可行方案**，审批仍执行原有权限、版本与快照检查。无有效结论、运行失败、授权失效、缺料对象不可计算或没有可行方案时转人工；授权失效时不交付受限报告。

旧持久化报告继续按原格式展示，无需迁移报告数据；新模型提交必须使用结构化协议。引用检查只验证来源与协议，不自动证明自然语言结论成立。数量、费用和可行性始终以确定性计算为准。协议及兼容说明见 [调查提示词与报告协议](docs/agent-prompt-engineering.md)。

## 目录与数据存储

```text
Scope/
├── src/                    React 页面、路由及样式
│   ├── components/         登录、注册、找回密码和账号页
│   └── supply/             助手、风险、调查审批、数据与权限页面
├── backend/                FastAPI、业务计算、LangGraph、权限及 Python 测试
│   ├── evals/              固定评测案例
│   └── eval_supply_agent.py 离线／真实模型评测及人工审核入口
├── project/                Agent 核心（按阶段迁移）
│   ├── agents/             报告结构与证据引用协议
│   ├── context/            已授权任务数据的纯上下文组装
│   ├── prompts/            提示词与内容版本加载器
│   └── tests/              迁移专项检查
├── tests/                  Playwright 页面测试与真实后端联调
├── scripts/                后端启动、模拟数据生成及历史存储维护
├── server/                 旧 Express 认证回退与历史对象存储工具
├── public/                 当前页面静态资源
├── assets/                 原有资源目录
├── docs/                   专题文档，含历史订单分析记录
├── plan/                   按时间记录的实现变更与验证结果
├── data/                   本机数据库、场景说明、服务、日志及历史备份
├── dist/                   Vite 构建产物
├── test-results/           浏览器测试结果与截图
├── .venv/                  本机 Python 虚拟环境
├── node_modules/           Node.js 依赖
├── .env.example            环境配置模板
├── requirements.txt        Python 直接依赖约束
└── requirements.lock.txt   Python 固定版本依赖清单
```

| 存储 | 内容 |
| --- | --- |
| `data/auth.sqlite` | 账号、验证码、会话等认证数据 |
| `data/supply.sqlite` | 上传原文件与记录、治理规则、助手会话、监测配置、工作空间权限及工具审计 |
| `supply-graph.sqlite` | LangGraph 检查点，实际位于 `DATA_DB_PATH` 同一目录 |
| PostgreSQL `supply_demo` | 固定模拟场景源数据；应用计算只读 |
| PostgreSQL `sc` | 扫描、风险、交期变更、调查、候选方案和审批决定 |
| 历史 SQLite／PostgreSQL／S3 | 已移除订单分析功能留下的数据及报告 |

当前仅支持单个后端应用进程，不使用多个 Uvicorn worker。部署或迁移时，认证库、业务 SQLite、LangGraph 检查点与 PostgreSQL 业务数据需配套备份；运行中的 SQLite 应使用一致性备份方式，不能只复制主文件而忽略未合并的 WAL。

`npm run storage:backup`、`scripts/restore-storage.mjs` 和 `scripts/verify-backup.mjs` 是历史 PostgreSQL/S3 工具，依赖原 `analyst_artifacts` 等表和对象存储配置，**不是当前应用全部数据的一键备份方案**，不会打包上述本地 SQLite。

## 验证命令

类型检查、构建和不依赖 PostgreSQL 的后端回归：

```powershell
npm run lint
npm run build
Remove-Item Env:SCOPE_DATABASE_INTEGRATION -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s backend -t . -p 'test_*.py' -v
```

`npm run lint` 实际执行 `tsc --noEmit`。未设置 `SCOPE_DATABASE_INTEGRATION=1` 时，数据库集成测试会跳过，不能将其视为完整业务验证。

仅运行使用模拟 API 的浏览器测试：

```powershell
npx playwright install chromium
npx playwright test tests/auth-design.spec.ts tests/assistant.spec.ts tests/data-connections.spec.ts tests/supply.spec.ts tests/workflows.spec.ts tests/investigation-report.spec.ts
```

完整业务回归需先准备 PostgreSQL 场景、可读写 `sc` 的应用连接，以及 SQL 测试所需的专用只读角色／视图：

```powershell
$env:SCOPE_DATABASE_INTEGRATION = '1'
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s backend -t . -p 'test_*.py' -v
npm run test:supply
```

Playwright 使用 3010 端口的测试前端；`tests/supply-live.spec.ts` 另外启动 3012 端口的临时后端，当前直接使用 Windows `.venv/Scripts/python.exe`。真实联调使用临时 SQLite 和独立测试工作空间，但需要可用 PostgreSQL，不受 Python 的集成测试开关控制。模型测试使用模拟响应，不代表已验证外部模型服务的连通性或输出质量。

### Agent 离线评测与真实模型评测

默认评测不调用外部模型，也不查询生产业务库：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m backend.eval_supply_agent
```

24 个固定案例覆盖范围限制、供应商陈述、提示注入、SQL 修复、截断结果、驳回、预算、无证据及伪造引用。结果默认保存到 `test-results/agent-eval-offline.json`，包含提示词版本、用量、延迟与无效调用记录。离线模拟用量不代表实际计费，协议检查通过也不代表真实模型语义质量通过。

需要检验已配置模型时，显式运行以下命令（会调用外部模型并可能产生费用）：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m backend.eval_supply_agent --live --output test-results/agent-eval-live.json
```

可加 `--case agent-supplier` 只运行一个案例。真实模型评测仍使用隔离的内存数据库固定样本，不替代 PostgreSQL 集成测试。结果需人工审核结论是否受证据支持、答案是否正确、条件是否完整保留，分别填写 `claimsSupported`、`answerCorrect`、`conditionsPreserved`；未审核时保留未评定状态。通过 `--review-result` 与 `--reviews` 可向已保存结果应用审核，审核须匹配该输出的 `outputHash`，不会再次调用模型。完整命令与审核格式见 [调查提示词与报告协议](docs/agent-prompt-engineering.md)。

## 文档索引与历史范围

| 文档 | 内容 |
| --- | --- |
| [后端说明](docs/fastapi-backend.md) | 启动、认证、静态托管和兼容入口 |
| [数据导入与治理](docs/data-import-governance.md) | 文件格式、质量检查、规则与复检 |
| [供应链分析接入](docs/supply-analysis.md) | 助手查询、澄清、模型解析和会话接口 |
| [聊天动作与调查衔接](docs/chat-actions.md) | 解释、刷新、多风险选择、调查试算与聊天审批 |
| [缺料计算与自动风险监测](docs/shortage-monitoring.md) | 计算口径、回放时点和风险生命周期 |
| [调查编排、方案试算与业务审批](docs/investigation-planning-approvals.md) | 试算、审批恢复、自动重规划及业务边界 |
| [角色与权限](docs/business-access.md) | 多角色、工厂／数据域授权、额度、迁移与审计 |
| [Agent 只读 SQL 调查](docs/investigation-sql.md) | 专用数据库角色、查询工具、证据及调用审计 |
| [调查提示词与报告协议](docs/agent-prompt-engineering.md) | 结构化结论、阶段约束、上下文、提示词版本与模型评测 |
| [供应链工作空间说明](docs/supply-workspace.md) | 公共布局和早期页面设计背景 |
| [模拟数据库说明](data/supply-demo/数据库使用说明.md) | 场景、数据表、查询示例和生成校验 |
| [代码修改记录](plan/) | 按时间记录的修改范围、实现边界及当次验证结果 |

部分专题文档保留分阶段交付描述，例如“审批尚未接入”“权限属于后续能力”或“页面尚未连接数据库”，以及助手查询固定等待确认的旧流程。这些描述已被后续实现更新；当前能力以本 README、对应最新专题和代码为准，权限以 [角色与权限](docs/business-access.md) 为准。

订单与支付分析、DataCo 分析页面及 `/api/analyst/*` 接口已移除。相关旧文档、历史数据库、S3 报告和相邻 `order-payment-analyst` 项目仅保留为历史资料；旧文档中的 `test:analyst`、`storage:migrate`、`storage:verify` 等脚本已不在当前 `package.json` 中。`metadata.json` 也保留原 OPENHORSE 原型描述，不代表当前产品范围。

`docs/investigation-progress.md`、`docs/verified-investigation-report.md`、`docs/deepseek-runtime.md` 和 `docs/model-chat-planning.md` 属于历史订单分析实现，不能将其中的预算、报告协议或工具名称直接用于当前供应链 Agent；当前以 `docs/agent-prompt-engineering.md` 与 `docs/investigation-sql.md` 为准。

`npm run start:legacy` 仅保留旧 Express 认证回退（需要 Node.js 24+），不提供当前完整供应链 API；日常开发与运行使用 FastAPI 入口。

Agent 目录迁移状态见 [迁移记录](docs/agent-migration/status.md)。新增专项测试运行：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s project/tests -t . -p 'test_*.py' -v
```

M0—M5b 已完成：提示词加载器在 `project/prompts/loader.py`，上下文及调查初始消息组装在 `project/context/investigation.py`，模型报告协议在 `project/agents/report_protocol.py`，Agent 判断循环在 `project/agents/investigator.py`；工具分发、SQL 校验与调用保护位于 `project/tools/`，LangGraph 编排和恢复位于 `project/workflows/investigation.py`。后端保留 API、模型请求、授权、业务事务、数据库连接、审计和检查点资源组装。旧检查点路径、节点和状态保持兼容；评测入口及旧测试尚未整体迁移。两组 Python 测试需分别运行，不能用后端测试发现命令代替 project 专项检查。
