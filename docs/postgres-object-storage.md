# PostgreSQL 与对象存储

> 历史说明：订单分析已从当前应用移除。本文的迁移及分析验证命令不再适用；数据和手动备份、恢复工具继续保留。日常应用启动无需 PostgreSQL/S3，参见 [当前后端说明](fastapi-backend.md)。

Scope 的分析工作区使用 PostgreSQL 管理会话、任务、检查点、工具调用、模型用量和文件索引。私有 S3 桶保存报告 ZIP、Markdown、SVG 及超过 64 KiB 的证据。文件按任务与 SHA-256 寻址，读取时验证哈希；浏览器不能直接指定对象 key，也不持有存储密钥。历史会话接口仍返回原有格式。

## 本机安装

- PostgreSQL 17.9：`127.0.0.1:55432`，数据库 `scope_analyst`。
- SeaweedFS 4.48：S3 `http://127.0.0.1:19000`，私有桶 `scope-artifacts`。
- 物理数据与程序：`Scope/data/services/`。Windows PostgreSQL 通过 `C:\ScopeLocalServices` 英文目录联接访问，避免中文路径初始化问题。此联接指向上述目录，不能当作独立备份。
- PostgreSQL 数据：`data/services/pgdata/`；对象数据与元数据：`data/services/seaweed-data/`。
- 应用连接信息在本机 `.env`；本机服务管理凭据在 `data/services/local-storage.json`，均不提交代码。应用使用专用 PG 角色与仅限本桶的 S3 凭据。
- 所有存储监听地址为回环地址。本机 HTTP 是开发部署配置，不是公网部署配置。

第一次安装的二进制来源：EDB Windows PostgreSQL binaries、SeaweedFS 官方 GitHub 4.48 release。之后运行 `node scripts/local-storage.mjs --setup` 初始化。该命令会写入本机存储配置，不用于已有云服务。

重启电脑后，在 Scope 目录执行：

```powershell
npm run storage:start
npm start
```

或执行 `scripts/start-local.ps1`。当前未注册开机服务或计划任务。

## 数据表与访问

| 表 | 用途 |
| --- | --- |
| analyst_sessions | 用户所属会话、已接受范围、待澄清内容 |
| analyst_jobs | 执行中/中断/完成任务及恢复原请求 |
| analyst_tasks | 每轮输入、结果摘要和对象引用 |
| analyst_checkpoints | 调查恢复状态、原预算、时限与版本 |
| analyst_tool_calls | 每次工具调用的参数、状态、结果或引用、尝试记录 |
| analyst_model_calls | 每次模型请求状态、已知 Token 数及错误信息 |
| analyst_artifacts | 文件 key、哈希、大小与类型 |
| analyst_migrations | 已完成的迁移标记 |

登录后可调用 `GET /api/analyst/tool-calls?session_id=...&tool=...&status=failed`，支持 `task_id`、`limit`（最多 100）。查询始终限制为当前用户。网页原有证据展开与历史回放继续可用；未新增专门的日志检索页面。

运行时通过 PG 会话锁防止跨进程重复执行；每次保存检查点时，工具/模型记录在同一事务内提交。S3 文件先上传，再提交引用。上传失败不会提交完成结果；失败上传可能留下无引用对象，目前保留，不自动删除。调用记录的时间表示入库/更新时刻，不伪造历史调用耗时。

分析快照仍为只读本地数据，登录认证仍使用原 `auth.sqlite`；本次迁移范围是分析会话和 Agent 存储。迁移不改变指标口径或 DeepSeek 配置。

## 迁移与验证

先停止 Scope 后端及其他调查进程，再运行 `npm run storage:migrate`。程序使用 SQLite 备份 API 留存两份源库，逐条迁移并验证对象回读，保持任务 ID、日期、检查点版本、预算和截止时间。空目标库才允许首次迁移；成功迁移再次运行只返回 already_migrated。源 SQLite 不删除。

`npm run storage:verify` 创建一次性 PG schema，验证真实查询、S3 往返、鉴权隔离、幂等、跨连接锁、存储故障、中断恢复、未知用量和过期预算。模型使用模拟响应，不产生 DeepSeek 费用。验证产生的对象保留为无引用对象，测试表在结束后删除。

## 协调备份与恢复

`npm run storage:backup`：在同一 PostgreSQL MVCC 快照中生成数据库 dump 和对象清单，再复制并验证该清单中的不可变对象；最后写出 `manifest.json`。没有该完成清单的目录不能视为成功备份。并发新任务可继续执行。不要给桶设置自动删除策略，否则可能破坏备份或历史引用。

备份默认在 `data/backups/postgres-s3-*`。也可运行 `node scripts/backup-storage.mjs <其他磁盘目录>`。目前提供手动备份命令，未启用自动定时备份或保留期清理；单机单盘也不具备高可用。

恢复时，准备一个**空 PostgreSQL 数据库**和私有桶，通过环境变量指定目标连接后执行：

```powershell
node scripts/restore-storage.mjs <完整备份目录>
```

先校验 dump 与所有对象哈希，上传对象，再单事务恢复数据库；拒绝覆盖非空数据库。还需单独备份认证库、业务快照与 `.env`，才能恢复整套应用。

云部署时更换 `.env` 的 PostgreSQL URL、S3 endpoint/bucket/region/凭据即可；使用 TLS、托管备份和受控权限。`ANALYST_PG_DUMP` / `ANALYST_PG_RESTORE` 可指定兼容版本客户端路径。本机服务脚本只用于本机安装。
