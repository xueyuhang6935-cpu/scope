# Scope 项目协作约定

## 开始工作

- 使用简体中文说明变更、验证结果和限制。
- 先阅读 `README.md`、相关实现，以及 `docs/logistics-execution/baseline.md` 和 `task-status.md`。基线是历史核查记录，当前事实需结合代码与实际检查确认。
- 修改前运行 `git status --short`，保留用户已有改动。不要执行清空、硬重置或覆盖现有配置。
- 按用户指定的任务范围实施；未要求时不自动推进下一阶段。优先复用既有模块，不新增无必要的框架、依赖或抽象。

## 工程入口

- 前端：React + TypeScript + Vite，入口 `src/main.tsx`、`src/App.tsx`，当前业务页面位于 `src/logistics/`；`src/supply/` 中的原版导航、编辑器、通用组件和空数据页面在正式前端复用；禁止恢复旧示例记录、模拟场景或供应链接口。
- 后端：`scripts/start-backend.mjs` → `python -m backend` → `backend/main.py:create_app`，使用 FastAPI。
- 正式入口仅认证和 `/api/logistics/`，授权在 `logistics_access.py`、历史源预览在 `logistics_data.py`。`create_legacy_test_app` 仅供 `NODE_ENV=test` 隔离回归，不作为运营入口。
- 调查 API：`backend/investigation_routes.py`；授权、审批事务与调查持久化：`backend/investigation_service.py`、`business_access.py`。`backend/supply_workflow.py` 组装业务服务与检查点，`investigation_agent.py`、`investigation_harness.py` 注入模型与审计能力。
- Agent 核心：`project/agents/`、`tools/`、`workflows/`、`context/`、`prompts/`；完整函数映射见 `docs/agent-migration/function-map.md`，兼容入口见 `docs/agent-migration/compatibility.md`。
- 依赖版本以 `package-lock.json` 和 `requirements.lock.txt` 为准。复用 `.venv` 与已安装依赖，不擅自批量升级。
- 维持单应用进程；增加 worker 前必须处理 SQLite、任务领取和 LangGraph 检查点的并发边界。

## 文件与模块边界

| 位置 | 职责与修改边界 |
| --- | --- |
| `src/components/` | 登录、注册和账号等通用界面；物流业务逻辑放在 `src/logistics/`，不要混入认证组件 |
| `src/logistics/` | 单空间页面与请求封装；权限由后端裁定，不展示旧场景示例 |
| `src/supply/` | 业务页面、展示状态和交互；通过现有 `supplyApi.ts`、`dataApi.ts` 等请求封装访问后端，不连接数据库、不持有服务端密钥、不自行裁定授权或权威指标 |
| `backend/main.py`、`auth.py`、`business_access.py`、`data_access.py` | 应用组装、认证与授权；场景功能通过既有入口接入，不为物流重写整套认证，也不绕过资源权限校验 |
| `backend/database_connection.py`、`data_store.py` 及相关业务模块 | 数据访问与存储；SQL 使用参数化和受控对象名，业务读取经过授权及快照边界。现有文件并非严格分层，新增访问逻辑就近复用，不为套目录结构做无关迁移 |
| `backend/shortage.py`、`risk_monitor.py`、`supply_planning.py` | 既有供应链计算与方案；保留原业务口径，物流指标和快照用独立职责实现，不能只替换字段名或前端文案 |
| `backend/investigation_routes.py`、`investigation_service.py` | 薄 API 与业务服务；身份、范围、授权、模型尝试预留、审批事务和提交后调度由后端负责，不下放给提示词 |
| `backend/supply_workflow.py`、`investigation_agent.py`、`investigation_model.py`、`investigation_sql.py`、`investigation_harness.py`、`investigation_audit.py`、`investigation_report.py` | 宿主能力组装、模型配置/HTTP、受限连接、审计存储及业务方案阻断；模型不能绕过工具执行 SQL 或业务写入；检查点路径与先停线程再关连接的顺序保持兼容 |
| `project/agents/` | 判断循环、预算收尾、结构化报告和引用校验；接收注入能力，不直接读取配置、连接数据库或导入后端 |
| `project/tools/` | SQL 协议与校验、调用保护、证据分发；连接、授权事实和审计存储由宿主注入，不复制业务授权实现 |
| `project/workflows/` | 图节点、分支、人工等待与恢复；业务事务和检查点资源由后端提供，修改时检查节点名、状态及 thread_id 兼容性 |
| `project/prompts/` | 行为与输出规则、模板加载及内容版本；提示词不能代替确定性校验，不加入冻结评价答案 |
| `project/context/` | 当前任务的已授权数据摘要和消息组装；不查库，后端先核对来源快照及实时权限 |
| `project/skills/`、`project/memory/` | 分别用于可复用任务方法和长期有用信息；目前仅职责说明，不自动加载为能力，不存秘密、运行数据库或评价答案 |
| `backend/*schema.sql` 及数据库初始化/迁移代码 | 表结构和兼容迁移；变更须评估已有数据、幂等及恢复方式，不删除源表或以重建库代替迁移 |
| `scripts/`、`server/` | 前者为启动、导入和维护脚本，后者主要为旧 Express/对象存储兼容实现；新业务 API 放在 FastAPI。只有任务涉及旧兼容功能时才修改 `server/` |
| `backend/test_*.py`、`project/tests/`、`tests/` | 通用后端测试、Agent 核心与宿主集成测试、浏览器测试；`project/tests/integration/` 可导入后端，旧 backend Agent 测试路径不增加重复发现包装 |
| `project/evals/`、`backend/eval_supply_agent.py` | 固定案例、评分和人工审核归 evals，配置/模型适配与 CLI 留后端；不把冻结评价答案放入提示词、流程手册或运行上下文 |
| `docs/`、`plan/` | 当前使用说明、实施契约和历史变更记录；更新受影响文档，旧记录通过新记录补充，不改写历史测试结果 |
| `data/`、`.env`、`.venv/`、`node_modules/`、`dist/`、`test-results/` | 本地数据、配置、依赖和生成产物；不作为源码编辑或提交。构建和测试按需生成产物，不手改产物来修复源码问题 |

- 跨模块修改前查找调用方与共享契约；接口字段、状态枚举、证据结构发生变化时，同步检查后端、前端请求类型、调用方及相关测试，不能只改一个页面。
- 新增物流模块前先定位可复用代码；允许按职责增加少量文件，不预建空目录、通用框架或重复服务。共享模块的改动要验证原供应链调用路径。
- 项目外的相邻工程和数据目录不属于默认修改范围；可以按任务读取相关材料，确需修改时应有明确的任务依据。

## 兼容入口

- 新的纯核心调用使用 `project` 实现。`backend.supply_prompts`、`supply_context` 的纯摘要导出、`investigation_report` 的协议导出采用显式转发，不复制实现、不使用通配导出；业务意图解析和方案阻断仍留后端。
- `backend.investigation_sql`、`investigation_harness`、`investigation_agent`、`supply_workflow` 是必要宿主适配器，不能作为空壳删除。保持原构造/调用契约；应用入口仍由后端组装。
- 评测命令保持 `python -m backend.eval_supply_agent`。案例位于 `project/evals/supply_agent_cases.json`；核心不读取 `.env`，仅宿主显式 `--live` 分支加载模型配置。
- 运行时核心和评测核心不导入 backend/FastAPI；测试允许验证后端集成。兼容入口删除前必须核查内部及外部调用方、明确替代入口并完成相关验证。

## 新增或升级依赖

1. 先确认标准库、平台能力或已有依赖不能合理完成需求，记录新增/升级原因与受影响模块。只调整目标包和必要的传递依赖，不执行无差别升级或 `npm audit fix --force`。
2. Node.js 使用 npm：新增/升级运行依赖用 `npm install 包名@目标版本`，开发依赖加 `--save-dev`，删除用 `npm uninstall 包名`。命令中的包名和版本须替换成实际值；同时提交 `package.json` 和 `package-lock.json`，不手改锁文件中的版本、完整性摘要或解析结果，不另引入 Yarn/pnpm 锁文件。
3. Python 的直接依赖约束维护在 `requirements.txt`，完整固定版本维护在 `requirements.lock.txt`。当前没有专用锁生成器，不假定存在 `pip-compile` 或 `uv` 工作流。使用与项目一致的 Python，在独立临时虚拟环境中先安装现有锁文件，再修改并解析目标依赖；升级用明确目标版本，删除时同步检查不再需要的传递依赖。
4. Python 解析成功并通过 `python -m pip check` 后，从该干净环境运行 `python -m pip freeze` 生成完整固定版本清单，以 UTF-8 更新 `requirements.lock.txt`；不能从混有个人工具的全局环境导出，也不要用 PowerShell 默认重定向生成 UTF-16 文件。审查清单，不纳入凭证、绝对本机路径或无关包。若移除依赖，确保清单没有残留仅为该包服务的传递依赖。
5. 审查清单与锁文件差异，解释必要的传递依赖变化。Python 固定版本必须满足 `requirements.txt`；记录运行时版本要求、API 兼容变化及迁移影响。直接依赖约束未变但锁定版本变化时，也应说明原因。
6. 验证可重建性：Node.js 在隔离副本执行 `npm ci`；Python 在另一个空虚拟环境仅从新锁文件安装，执行 `python -m pip check`，再用 `python -m pip install --dry-run -r requirements.txt` 核对无需新增或改变已锁版本。安装失败或声明与锁不一致时，不能宣称依赖升级完成。
7. 验证行为：前端或构建依赖变更运行 `npm run lint`、`npm run build` 及受影响的浏览器测试；后端依赖变更运行相关后端测试。FastAPI、数据库驱动或 LangGraph 等核心依赖还需启动冒烟及适用的接口、持久化/恢复测试；模型相关依赖需离线协议检查。外部服务缺失时如实记录未验证项，不能用模拟通过代替真实集成结果。
8. 在同一次变更中交付依赖声明、锁文件、必要的兼容代码与文档，以及 `plan/` 记录；注明实际执行命令、结果和剩余限制。失败时保留用户原环境及配置，不覆盖现有 `.venv` 来反复试错。

## 数据与物流契约

- 上海 LaDe 数据已有导入和预览实现；先核对实际源库，不默认重新下载、重建或重新导入。
- 源记录保持不变，新增业务实体通过适当迁移管理。不要把旧供应链模拟场景当作物流数据。
- 2000 年只是参考时间轴，真实年份未知；不能据此推断星期、节假日或真实天气。`accept_time` 是快递员接单时间，`ds` 是完成日期。
- 风险快照只允许 `accept_time <= T` 的任务可见，完成信息仅在 `pickup_time <= T` 时可见；GPS 采样也必须不晚于 T。不能按未来完成日期筛选候选任务。
- 待揽收任务隐藏最终完成时间、时长、超时标签、完成日期和未来派生标记。数据管理预览不能直接作为 Agent 快照输入。
- 数量、时长和排序由程序计算；结论带证据，区分程序计算、观察、人工陈述和推断。
- 范围、业务时点、权限、审批版本和预算由后端校验。不能用前端隐藏或模型提示词代替授权。
- 发起人不能自批；批准不等于执行。首版内部跟进不能冒充真实派单，历史完成不能归因于模拟建议。

## 配置与版本控制

- `.env` 只在本机保存；不输出或提交密码、令牌、连接字符串及私钥。`.env.example` 只包含占位配置。
- 不提交 `.venv/`、`node_modules/`、`data/`、数据库文件、`dist/`、缓存、日志、测试产物或本地备份。
- `main` 保留稳定基线，功能工作使用独立分支。提交前查看暂存差异；提交者姓名和邮箱使用用户提供的身份，不编造。
- 远程发布或推送按用户明确要求执行；本地 Git 不等于已备份到远程，也不覆盖数据库灾备。

## 验证与交付

在 Scope 目录使用现有命令，按改动范围选择：

```powershell
npm run lint
npm run build
Remove-Item Env:SCOPE_DATABASE_INTEGRATION -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s backend -t . -p 'test_*.py' -v
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s project/tests -t . -p 'test_*.py' -v
.\.venv\Scripts\python.exe -X utf8 -m backend.eval_supply_agent
```

- Python 完整回归必须运行 backend 和 project 两组发现命令；只运行 backend 会漏掉 Agent 测试。测试分类见 `project/tests/README.md`，评测与人工审核见 `project/evals/README.md`。
- 浏览器测试使用 `npx playwright test`，当前为认证与物流界面。七份旧供应链 spec 已改名为 `*.legacy.ts` 留档；旧数据库测试须有独立旧页面和隔离数据库才可重新启用。离线模拟通过不证明真实模型或数据库流程通过。
- 数据库集成仅在确认隔离测试库、所需场景和角色就绪后启用 `SCOPE_DATABASE_INTEGRATION=1`。不要为了让测试通过向当前源库重建旧场景。
- 旧核心隔离前提就绪后可设置 `$env:SCOPE_DATABASE_INTEGRATION = '1'`，重新运行上述两组 Python 发现命令，结束后移除该环境变量。当前界面使用 `npm run test:logistics`；旧页面不再提供运行入口，不向源库重建测试场景。
- 实际运行过的检查才写通过；跳过、未运行、已有失败和新失败分开记录。历史失败见 T01 基线，不通过删除断言或放宽权限掩盖问题。
- PowerShell 读取中文文档和路径清单时显式使用 UTF-8。测试启动优先隔离 SQLite、关闭自动监测与不必要的模型调用，退出时清理自己创建的进程树。
- 完成物流任务后更新 `docs/logistics-execution/task-status.md` 和相关证据。代码、配置或测试变更在 `plan/` 新增简体中文时间戳记录，写清变更、实际验证和限制；不覆盖旧记录。
