# T03：快照服务与字段掩码

实现范围：物流 Repository、后端专用只读源账号、快照及行表的幂等迁移、创建与受控分页读取。复用既有单空间成员及城市/区域授权，不新增默认授权，不修改历史源记录。后续 T04 [指标和风险排序](t04-metrics.md) 已实现；T05 工作台及完整 G1 验收、T07 模型参数化工具仍按后续任务实施。

## 接口

登录后创建：`POST /api/logistics/snapshots`。

```json
{
  "batch_id": "已保存的核验批次编号",
  "city": "Shanghai",
  "region_ids": ["5"],
  "business_as_of": "2000-07-02 10:00:00"
}
```

返回固定快照元数据，包括 `snapshot_id`、版本、时点、范围、`visible_content_hash`、`snapshot_fingerprint`、覆盖限制和 `ready` 状态。真实创建时间与参考业务时间分开。2000 是参考年份，不代表真实年份。

读取：`GET /api/logistics/snapshots/{snapshot_id}?page=1&page_size=20`。每页最多 100 行，返回 `snapshot`、掩码后的 `rows`、总数、返回数、截断标记和 `ok/empty` 状态。读取不能覆盖时点、批次、城市或区域。没有更新接口，条件改变须创建新快照。

创建和读取均核对账号是否存在、启用成员、工作空间、已保存批次及明确区域授权。多个业务角色的可读区域可合并；混合授权/未授权区域整体拒绝。系统管理员无业务权时不能创建或读取。构建结束再次检查授权和权限版本；撤权后旧快照及持久化内容不能继续读取。下游宿主应调用 `SnapshotService.read`，不得绕过它直接读取快照表；模型只接收宿主绑定的快照 ID。当前尚未连接模型工具。

## 数据边界与存储

- Repository 仅使用 `LOGISTICS_SOURCE_DATABASE_URL`，缺少配置或权限不合格时明确不可用，不回退到 `ANALYST_DATABASE_URL` 或 Agent 连接。
- 严格解析源文本为参考时间，在授权批次、城市和区域内按 `accept_time <= T` 选任务，保留跨日任务。不用完成日期、最终资格或质量标签筛选。
- 完成 `<= T` 才返回完成时间和完成日期。事件发生且 GPS 采样 `<= T`、完整且坐标有效才成组返回 GPS；不可见采样与原始缺失使用相同可见质量说明。
- 用白名单构造新对象；原始 JSON、最终时长/超时标签、完整质量数组、质量统计和绝对源路径不进入快照。要求窗口作为接单时已知承诺可见。日期或身份冲突明确报错，错误正文不回显源值。
- 掩码后的业务行存于 `logistics_snapshot_rows`，按稳定业务键排序。元数据和 `building/ready/failed` 存于 `logistics_snapshots`；行写入与就绪状态原子提交，失败快照不允许查询。
- T03 初始快照使用 `metric_version=null`；T04 接入后新快照绑定 `logistics-metrics-v1`，历史 null 版本快照不回写，指标响应显式标注当前计算版本。每次创建独立 ID，不引入缓存复用。内容摘要只依据可见行，溯源指纹另绑定批次版本、时点和范围。
- 首版按授权范围流式读取源记录，可见行在内存组装；全市快照的峰值内存和耗时尚未压测。当前支持单应用进程。

数据管理员的既有历史源预览继续作为独立管理通道；调查员和审核人不能通过该通道读取完整历史源。原始预览不会作为 Agent 快照输入。

## 专用只读账号

本机安装：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m backend.install_logistics_source --local
```

仅允许项目自带 `127.0.0.1:55432/scope_analyst`；使用已有本机管理员配置安装 `scope_logistics_source_reader`，只授权 `lade` 使用和 `pickup_tasks/import_batches` 读取。保持应用写入账号、Agent 账号和源读取账号分离，本机 `.env` 仅追加专用连接键，不输出凭证。重复安装保留已有专用密码。角色不得有成员关系、超级权限、建库/建角色/绕过 RLS 权、业务 schema 创建权或源表写权限；运行时仍核验权限。验证中的 DDL 拒绝指业务 schema 建表，不宣称禁止 PostgreSQL 默认临时对象功能。

其他环境先设置 `LOGISTICS_SOURCE_ADMIN_DATABASE_URL`、`ANALYST_DATABASE_URL`、`LOGISTICS_SOURCE_READER_PASSWORD`，运行不带 `--local` 的同一命令，再配置 `LOGISTICS_SOURCE_DATABASE_URL`。已有 PUBLIC 继承授权导致权限门禁不通过时，由数据库管理员按当地策略处理，不自动更改所有账号的公共授权。

升级后重启后端会自动执行幂等 SQLite 建表，不重建数据库、不删除已有记录；已运行的进程需重启才能加载新增接口和连接配置。

## 验证与复跑

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest backend.test_logistics_snapshots backend.test_logistics -v
.\.venv\Scripts\python.exe -X utf8 scripts/verify-logistics-snapshots.py
```

专项测试验证精确字段、事件边界、跨日、GPS 组合门禁、未来合法事件修改不影响可见内容和摘要、错误脱敏、账号/区域/批次约束、构建中撤权及授权版本变化、空快照和失败快照。

真实源验证使用临时认证/业务 SQLite 和区域 5 的独立授权账号，通过真实 HTTP 创建快照；独立 SQL 核对三个时点的可见与已完成任务数。关闭源账号事务只读默认后仍拒绝无匹配 UPDATE、业务 schema DDL 和 SET ROLE；Agent 源表读取仍拒绝。临时业务库重启后核验已保存快照和撤权，源批次存储的内容摘要前后相同。未对全源重新计算内容摘要，不把“存储摘要不变”冒充另一次全量内容审计。

最终证据：[真实源快照验证](../../test-results/logistics-t03/20261007-final/snapshot-verification.json)。测试产物按仓库规则忽略。T03 完成不表示 T04—T05 的三指标独立复算、完整下载/日志/模型上下文不变性或 G1 已通过。
