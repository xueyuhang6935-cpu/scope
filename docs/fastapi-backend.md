# SupplyPilot 后端

HTTP 后端使用 FastAPI + Uvicorn，保留登录、注册、验证码、找回密码和微信登录接口。订单分析路由、桥接和运行存储已移除。

智能助手已新增 `/api/supply/*` 真实数据分析与持久化会话接口，支持库存、质量和缺料就绪检查。启动、可选模型配置、接口及限制见 [供应链分析接入](supply-analysis.md)。

## 安装和启动

```powershell
py -3.11 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
npm start
```

`npm run dev` 同时启动后端自动重载和 Vite。`scripts/start-local.ps1` 只启动后端，不再自动启动历史 PostgreSQL/S3。`SCOPE_PYTHON` 可以指定 Python，`AUTH_PORT`、`AUTH_HOST` 可以指定监听端口及地址。`DATA_DB_PATH` 可指定数据导入与治理的 SQLite 数据库，默认 `data/supply.sqlite`。

默认认证服务为 `http://localhost:3002`，开发前端为 3003。后端改用其他地址时，通过 `SCOPE_API_PROXY_TARGET` 指定前端代理。沿用本机已有 `.env`；IPv4 端口被占用时，可以使用现有 IPv6 配置或另外选择端口。

- 健康检查：`/api/health` 检查认证 SQLite，返回供应链工作空间标识。
- 接口文档：`/docs`，包含认证、导入和数据治理接口。业务接口约定见 [数据导入与治理说明](data-import-governance.md)。
- `/api/analyst/*` 返回 404，不再初始化或访问历史分析存储。
- 所有页面深链接通过 `index.html` 回退，可直接刷新。

## 账号兼容

账号和会话继续保存在 `data/auth.sqlite`，scrypt 密码格式及 `scope_session` Cookie 与旧服务兼容。退出、验证码及登录限流继续沿用原实现。当前账号设置页未接入资料编辑、2FA、通知配置和微信绑定接口，这些操作显示不可用。

`npm run start:legacy` 保留 Express 认证服务作为回退，同样不提供订单分析接口；回退服务需要 Node.js 24+。不同时在同一端口启动两个后端。

## 验证

```powershell
.venv/Scripts/python.exe -X utf8 -m unittest backend.test_backend backend.test_wechat_mock backend.test_data_import -v
```

验证使用临时账号库，覆盖密码兼容、注册、登录、密码重置、退出、请求边界、本地微信登录限制、旧接口移除以及无需业务存储即可启动。验证码投递使用模拟对象，不发送邮件或短信。

## 历史存储

原数据与配置保留，应用不执行迁移。手动备份和恢复工具继续使用原 Node PostgreSQL/S3 依赖。`docs/postgres-object-storage.md` 等旧文档为历史说明，其旧分析启动和验证命令已不适用于当前应用。
