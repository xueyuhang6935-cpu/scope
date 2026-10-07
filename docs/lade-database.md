# LaDe-P 上海揽收任务数据库

## 存储位置与回查

使用项目 `.env` 中已有 `ANALYST_DATABASE_URL`，目标 PostgreSQL 数据库为 `scope_analyst`。新增 `lade` 数据区，不复用供应链模拟表。

| 表 | 内容 |
|---|---|
| `lade.pickup_tasks` | 每条任务的原始 19 列、来源文件与行号、导入批次、质量标记、计算资格及相对时效结果 |
| `lade.import_batches` | 文件绝对路径、SHA-256、字节数、行数、导入和核验时间、官方来源及修订号、审计文件哈希、字段映射全文、审计摘要、全量内容摘要 |
| `lade.quality_rules` | 本批质量规则代码、中文说明、命中行数和处理方式 |

主键为 `(batch_id, source_row)`，保留原始 `order_id`，不生成替代任务编号。调查结论与证据建议携带 **batch_id、order_id、source_file、source_row、quality_flags**，从而定位特定版本原文件中的具体记录。

`source_row` 包含表头行，第一个任务为第 2 行。当前文件每条 CSV 记录占一行，因此它也是原文件物理行号。`source_file` 为 `data/pickup_sh.csv`，批次表另保存绝对路径及内容哈希。

原始 19 列全部使用 `text NOT NULL`，保留原始数值文本、ID、四位 `ds`、时间文本和 GPS 空字符串。不会把 `0708` 改成 `708`，不会用零或其他任务的坐标填补 GPS。分析时需要数值可使用 `NULLIF(lng,'')::numeric`；缺失检查应使用空字符串，而不是仅用 `IS NULL`。

质量字段独立使用 boolean、numeric、text[] 类型。`quality_flags` 为规则代码数组，标记非空不意味着必须排除任务：跨日、超时和提前完成都是需要保留的业务事实。基础计算筛选 `eligible_duration` 或 `eligible_sla`，空间计算再核对相应空间资格与复核字段。

## 年份和时间

原始时间保持 `MM-DD HH:mm:ss`，真实年份没有官方证据。批次 `actual_year=NULL`、`reference_year=2000`，任务 `year_status='undisclosed'`。时长和窗口超时分钟沿用上一轮审计的同年参考轴计算；没有将 2000 伪装成真实采集年份。当前批次覆盖 5—11 月，同年参考轴不改变时间差；不能据此关联真实年份的星期、节假日或天气。

## 查询示例

按任务编号回查原始任务和文件版本：

```sql
SELECT t.batch_id, t.order_id, t.source_file, t.source_row,
       t.courier_id, t.region_id, t.accept_time, t.pickup_time,
       t.time_window_start, t.time_window_end,
       t.quality_flags, t.eligible_sla, t.late_minutes,
       b.source_sha256, b.source_file AS absolute_source_file
FROM lade.pickup_tasks t
JOIN lade.import_batches b USING (batch_id)
WHERE t.city = 'Shanghai' AND t.order_id = '2590170';
```

查看一条任务所有质量标记的中文说明：

```sql
SELECT t.order_id, t.source_row, r.code, r.description, r.action
FROM lade.pickup_tasks t
JOIN lade.quality_rules r
  ON r.batch_id = t.batch_id AND r.code = ANY(t.quality_flags)
WHERE t.order_id = '2590170';
```

查看时效计算及 GPS 缺失的关系：

```sql
SELECT count(*) AS tasks,
       count(*) FILTER (WHERE eligible_sla) AS sla_eligible,
       count(*) FILTER (WHERE eligible_sla AND
         (accept_gps_time = '' OR pickup_gps_time = '')) AS gps_missing_sla_eligible
FROM lade.pickup_tasks;
```

按区域连续时间提取完整生命周期（当前批次月日格式固定，可同年字典序比较）：

```sql
SELECT * FROM lade.pickup_tasks
WHERE city = 'Shanghai' AND region_id = '5'
  AND accept_time < '07-15 00:00:00'
  AND pickup_time >= '07-01 00:00:00'
ORDER BY accept_time, order_id;
```

此查询保留期初积压和期末完成事件；如果还需包括已提前完成但要求窗口在区间内的任务，应另加入窗口相交条件。公开数据中的积压仅代表文件内可观测任务，不保证覆盖生产系统所有未完成或取消任务。

## 导入与验证

在工作区根目录执行：

```powershell
Scope\.venv\Scripts\python.exe -X utf8 Scope\scripts\import-lade.py
```

导入使用已有 psycopg 和 PostgreSQL COPY，不新增依赖。整个批次在一个事务中完成，任何输入行号、任务标识、来源版本、规则计数或回读摘要不符都会回滚。任务编号、区域接单时间、快递员有查询索引。

脚本复用 `outputs/lade_quality_20261006/` 中的全量审计结果，不临时修改质量规则。源 CSV 哈希必须与已审计版本一致，原始和质量判定文件必须逐行匹配。重复运行同一批次只核验，不重复插入或覆盖。

仅核验已入库批次：

```powershell
Scope\.venv\Scripts\python.exe -X utf8 Scope\scripts\import-lade.py --verify-only
```

验证覆盖全部原始字段、来源信息和质量派生列，从数据库按来源行号逐条读回并与输入规范化有序摘要比较。结果保存到 `outputs/lade_quality_20261006/PostgreSQL导入核验.json`。这里的只核验不会修改数据库，但会更新本地核验结果文件。

## 网页数据库接入（2026-10-07 更新）

数据中心的“数据库连接”已支持 LaDe。后端发现已核验的 LaDe 导入批次时优先展示 `lade`，没有 LaDe 批次时沿用供应链场景接口。首次自动探测最新批次，连接成功后即可只读预览；“保存并接入”将批次选择保存到当前工作空间。连接是否可用与选择是否已保存分别由 `connected` 和 `configured` 表达。

- `GET /api/data/database`：连接状态、批次、表列表、默认任务表。
- `POST /api/data/database/test`：测试当前批次任务是否可读取。
- `POST /api/data/database`：用 `scenarioId` 传递批次编号（沿用原接口参数名），保存到 SQLite `database_source_connections`。不会将物流批次写入缺料场景配置，也不会创建风险监测配置。
- `GET /api/data/database/preview`：默认读取 `pickup_tasks`，每页 20 条，按来源行号稳定排序；支持 `region` 区域编号和 `order_id` 任务编号精确筛选。另可预览 `quality_rules` 和 `import_batches`；批次预览不输出本机绝对路径及内部大体积快照。

页面区分“导入批次”和原有“数据场景”，不将公开揽收任务标为供应链模拟数据。物流任务预览不套用 A/B 工厂过滤。由于现有工厂权限不能代表物流区域权限，当前仅允许在同一角色内具备完整连接管理权限的数据管理员预览，普通只读成员或单厂管理员不能借此访问全区域任务；区域筛选是查询条件，不是授权范围。

遗留场景已经删除且无法解析时，默认空间允许回到本地空间重新接入，不删除旧配置。现有供应链缺料计算、Agent SQL 白名单、审批和执行流程尚未迁移到物流数据；本次接入成功不代表这些分析能力已经支持 LaDe。
