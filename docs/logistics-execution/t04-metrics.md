# T04：三核心指标与手算夹具

指标服务位于 `backend/logistics_metrics.py`，只读取 T03 已持久化的授权掩码行，不读取历史源表。新增三个 HTTP 查询入口，所有入口复用同一个实时授权及 ready 状态检查。参考业务时刻、批次和区域由快照绑定，不能通过查询参数覆盖。模型工具封装属于 T07，工作台和完整 G1 属于 T05。

## 指标及边界

口径版本为 `logistics-metrics-v1`，临期跨度 H=30 分钟，近期窗口 W=60 分钟。它们是项目设置，不是行业规范。

| 指标键 | 名称 | 口径与分母 |
| --- | --- | --- |
| `backlog` | 样本内积压 | 已接单且未观察到完成；汇总分母为可见任务总数 |
| `overdue` | 逾期未完成 | 积压中窗口终点严格早于 T；汇总分母为积压总数 |
| `due_soon` | 临期未完成 | 积压中 `T <= window_end <= T+30分钟`，两个端点都包含；汇总分母为积压总数 |
| `recent_accepted` | 近期接单 | 接单事件处于 `(T-60分钟,T]`；覆盖分母为可见任务总数 |
| `recent_completed` | 近期完成 | 已观察完成事件处于 `(T-60分钟,T]`，包含期初积压的完成；覆盖分母为可见任务总数 |

数量单位为 `task`。积压任务逐条返回 `current_wait_minutes`、`current_overdue_minutes`、`remaining_minutes` 和 `risk_state`；分钟保留精确秒换算的小数，分类前不取整。已知非逾期任务的当前逾期为 0；逾期任务的剩余分钟为负数。风险状态为 `overdue/due_soon/normal/window_unavailable`。

窗口起点或终点缺失时，积压和等待仍可计算；该任务的当前逾期/剩余分钟为 null，放到不可计算任务末尾。存在此类积压时，完整逾期及临期汇总 `value=null`、`status=unavailable`，提供 `known_lower_bound` 和 `missing_window_count`。不能把它们当成 0。没有可见任务为 `empty` 并带样本覆盖限制；已有可见任务且没有风险时，指标为可计算的 0。

默认任务列表仅包含积压：逾期优先、逾期分钟降序；其次临期、剩余分钟升序；然后普通、窗口终点升序；最后窗口不可计算。同分按 `(city,region_id,order_id,batch_id,source_row)` 稳定排序，编号保持字符串顺序。

## 接口

以下接口均需登录及快照全范围读取权：

```text
GET /api/logistics/snapshots/{snapshot_id}/summary
GET /api/logistics/snapshots/{snapshot_id}/tasks?risk_state=all&page=1&page_size=20
GET /api/logistics/snapshots/{snapshot_id}/groups?dimension=courier_id&page=1&page_size=20
```

- `summary`：返回五项指标、可见/已完成任务数、缺失窗口数；各指标包含名称、值、单位、分母和状态。
- `tasks`：风险类型可选 `all/overdue/due_soon/normal/window_unavailable`；全范围计算后过滤、排序、分页，不把分页返回数当风险总数。
- `groups`：只接受 `courier_id/aoi_id`，统计有积压任务的分组，按分组编号字符串稳定分页；返回每组积压、逾期和临期数量及 `share`。积压占比分母是全快照积压，逾期占比分母是全快照逾期，临期同理。不是组内逾期率；计数本身不等于因果或责任。
- 分组的某类全局风险数不可计算时，该类 `denominator/share=null`；某组自身有缺失窗口时其完整风险数量也为 null，另列已知下界。全局风险总数为 0 时该风险占比为 null，避免除零。
- 页码从 1 开始，每页 1—100 条。每个响应带 `snapshot_id/fingerprint/visible_content_hash/metric_version/denominator/unit/total_rows/returned_rows/truncated/status`，以及固定时点、范围和适用限制。汇总不返回任务行，`returned_rows=0`，`total_rows` 是可见任务数；列表和分组的总数分别是匹配任务数和分组数。分母始终来自全快照。

新快照绑定 `metric_version=logistics-metrics-v1` 并计入溯源指纹。历史 T03 的 null 版本快照允许使用明确标注的 v1 服务复算，但不改写原内容、元数据或指纹；未知非空口径版本拒绝。T03 的普通快照读取继续返回原掩码字段，T04 计算字段在指标任务入口返回。

单进程下计算持有业务存储共享锁，防止与成员授权更新交错。每次查询复算，首版不增加缓存或指标持久化迁移；只复制及排序积压任务。全市容量与高并发性能尚未压测。

## 手算与独立复算

夹具位于 `tests/fixtures/logistics-t04.json`，为执行方案 A—H 的人工任务，只属于开发测试，不进入真实源表、应用默认数据或冻结评价。要求窗口起点统一为终点前 60 分钟；测试分组编号也属于人工构造。

| 参考时点 | 可见 | 已完成 | 积压 | 逾期 | 临期 | 近期接单 | 近期完成 | 积压顺序 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `2000-07-02 10:00:00` | 7 | 2 | 5 | 2 | 2 | 4 | 1 | G→A→H→B→C |
| `2000-07-02 10:30:00` | 8 | 4 | 4 | 2 | 1 | 4 | 3 | G→B→E→C |

独立脚本 `scripts/reference_logistics_metrics.py` 仅用标准库，不导入被测快照/指标函数。它从人工原始任务重建可见集合，独立计算数量、分钟、排序和分组，再与固定手算答案交叉核对。也支持 `--input` 原始隔离记录 JSON 数组及 `--as-of` 指定时点；原始测试/参考输入只供授权宿主核查，不进入模型。

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest backend.test_logistics_metrics -v
.\.venv\Scripts\python.exe -X utf8 scripts/reference_logistics_metrics.py
.\.venv\Scripts\python.exe -X utf8 scripts/verify-logistics-metrics.py
```

真实核验脚本使用专用只读源和临时认证/业务 SQLite，通过 HTTP 创建区域 5、6 的 00:00、10:00、10:30 快照。独立参考从授权原始数据读取，服务只使用掩码持久化内容；核对五项指标、前 100 条任务排序与分钟、两类分组计数/占比/全局分母及跨日积压。另核对两区域共四个时点区间的 `B(T2)=B(T1)+接单(T1,T2]-完成(T1,T2]`。

证据：[手算独立复算](../../test-results/logistics-t04/hand-reference.json)、[两区域六时点真实核验](../../test-results/logistics-t04/20261007-213319/metric-verification.json)。核验结束撤权后，三个指标入口均拒绝。源批次存储摘要前后相同，未声称重新计算过全量源摘要。测试产物按仓库规则忽略。

专项还覆盖秒级边界、近期左开端点、零时长、缺失与可计算零值、空样本、字符串同分顺序、未来事件不变性、分页和查询参数/版本/权限门禁。完整下载、日志、模型上下文与前端工作台不在此次实现范围，不把这些专项验证当作完整 G1 已通过。
