# 首次运行

本文面向新克隆的仓库。历史文档中的本机账号、数据批次和测试结果不会随源码自动恢复。

## 环境与安装

推荐 Node.js 24+、Python 3.11+，使用 npm 和 Python venv。固定依赖分别记录在 `package-lock.json` 与 `requirements.lock.txt`；本次上传准备没有重新验证依赖安装。

macOS / Linux，在仓库根目录执行：

```bash
npm ci
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
# 仅在 .env 不存在时创建，保留已有配置。
test -e .env || cp .env.example .env
```

Windows PowerShell：

```powershell
npm ci
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

启动脚本优先使用项目 `.venv`，也可用 `SCOPE_PYTHON` 指定解释器，无需激活虚拟环境。

## 基础启动与登录

编辑 `.env`，将 `AUTH_SECRET` 替换为自己的随机长字符串，其余连接和密钥按需配置。`.env.example` 只提供占位值，不包含可用账号或服务。

```bash
npm run dev
```

| 地址 | 用途 |
| --- | --- |
| `http://localhost:3003` | 开发前端 |
| `http://localhost:3002/docs` | FastAPI 文档 |
| `http://localhost:3002/api/health` | 认证 SQLite 健康检查，不代表源数据库和模型已就绪 |

基础认证服务可以在未连接 PostgreSQL、未配置模型的情况下启动。注册和找回密码需要邮件或短信服务；本机体验可显式开启 `WECHAT_MOCK_LOGIN=true` 并使用页面微信登录入口。该模式只限本机且在生产模式禁用。

新用户没有默认业务授权。本地测试登录不会自动创建有业务权限的管理员、调查员或审核人。已有成员由系统管理员在 `/settings` 授权；全新空库目前没有独立的一键管理员初始化流程，完整业务演示需要先完成受控的成员初始化。不要复制他人的认证数据库或历史密码文件解决此问题。

构建后运行：

```bash
npm run build
npm start
```

此时由后端在默认 `3002` 端口托管构建页面和 API。保持单应用进程，不直接增加 worker。

## 物流数据准备

数据来自 [LaDe](https://huggingface.co/datasets/Cainiao-AI/LaDe)，上海揽收子集。历史记录为 1,424,406 条，不代表仓库包含这些数据。获取和使用数据须遵守原数据集条款。

完整调查需要以下前提：

1. PostgreSQL 中已有符合契约的 `lade.pickup_tasks` 与 `lade.import_batches`，并存在已核验批次。
2. `.env` 配置应用连接 `ANALYST_DATABASE_URL`，以及独立只读源连接 `LOGISTICS_SOURCE_DATABASE_URL`。快照服务不会回退到应用连接。
3. 数据管理员在数据中心保存批次连接；成员获得对应上海区域的业务授权。

现有 `scripts/import-lade.py` 依赖仓库外的 `../data/pickup_sh.csv` 和 `../outputs/lade_quality_20261006/` 审计材料，**仅克隆本仓库不足以执行该历史导入流程**。`--local` 只读账号安装还依赖本地数据库服务配置文件，不是通用首次安装命令。

当前不提供一键全量数据部署。请先阅读 [数据契约](logistics-execution/data-contract.md)、[快照契约](logistics-execution/snapshot-contract.md)、[LaDe 数据库说明](lade-database.md) 和 [只读源账号说明](logistics-execution/t03-snapshots.md)，按自己的环境准备数据，不向已有源库重复灌入旧模拟场景。

## 可选模型

在 `.env` 中配置 `SUPPLY_MODEL`、`SUPPLY_MODEL_BASE_URL` 和 `SUPPLY_MODEL_API_KEY`；这些历史命名由物流适配器复用。模型调查仍需在发起时显式选择，默认调查走确定性流程。聊天模型与调查模型的配置和预算说明见 [连续对话](logistics-execution/v2-chat.md) 与 [模型协议](logistics-execution/t10-protocol-model.md)。

## 检查命令

```bash
npm run lint
npm run build
npx playwright test
```

macOS / Linux 的 Python 离线回归：

```bash
env -u SCOPE_DATABASE_INTEGRATION .venv/bin/python -m unittest discover -s backend -t . -p 'test_*.py' -v
env -u SCOPE_DATABASE_INTEGRATION .venv/bin/python -m unittest discover -s project/tests -t . -p 'test_*.py' -v
.venv/bin/python -m backend.eval_supply_agent
```

Windows 命令及测试边界见 [协作约定](../AGENTS.md)、[核心测试](../project/tests/README.md) 和 [浏览器测试](../tests/README.md)。数据库集成只对隔离测试库开启，离线测试和协议评测不能证明真实模型质量。
