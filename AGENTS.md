# Scope 项目协作约定

## 开始工作

- 使用简体中文说明变更、验证结果和限制。
- 先阅读 `README.md`、相关实现，以及 `docs/logistics-execution/baseline.md` 和 `task-status.md`。基线是历史核查记录，当前事实需结合代码与实际检查确认。
- 修改前运行 `git status --short`，保留用户已有改动。不要执行清空、硬重置或覆盖现有配置。
- 按用户指定的任务范围实施；未要求时不自动推进下一阶段。优先复用既有模块，不新增无必要的框架、依赖或抽象。

## 工程入口

- 前端：React + TypeScript + Vite，入口 `src/main.tsx`、`src/App.tsx`，业务页面位于 `src/supply/`。
- 后端：`scripts/start-backend.mjs` → `python -m backend` → `backend/main.py:create_app`，使用 FastAPI。
- 调查、权限与审批：`backend/supply_workflow.py`、`business_access.py`、`investigation_agent.py`、`investigation_harness.py`。
- 依赖版本以 `package-lock.json` 和 `requirements.lock.txt` 为准。复用 `.venv` 与已安装依赖，不擅自批量升级。
- 维持单应用进程；增加 worker 前必须处理 SQLite、任务领取和 LangGraph 检查点的并发边界。

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
.\.venv\Scripts\python.exe -X utf8 -m backend.eval_supply_agent
```

- 浏览器测试使用 `npx playwright test` 加相关 spec 路径。`tests/supply-live.spec.ts` 依赖真实 PostgreSQL；离线模拟通过不证明真实模型或数据库流程通过。
- 数据库集成仅在确认隔离测试库、所需场景和角色就绪后启用 `SCOPE_DATABASE_INTEGRATION=1`。不要为了让测试通过向当前源库重建旧场景。
- 实际运行过的检查才写通过；跳过、未运行、已有失败和新失败分开记录。历史失败见 T01 基线，不通过删除断言或放宽权限掩盖问题。
- PowerShell 读取中文文档和路径清单时显式使用 UTF-8。测试启动优先隔离 SQLite、关闭自动监测与不必要的模型调用，退出时清理自己创建的进程树。
- 完成物流任务后更新 `docs/logistics-execution/task-status.md` 和相关证据。代码、配置或测试变更在 `plan/` 新增简体中文时间戳记录，写清变更、实际验证和限制；不覆盖旧记录。
