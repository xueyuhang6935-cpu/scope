# T07：首批参数化业务工具

完成日期：2026-10-08（北京时间）。实现版本：`T07-20261008`，分支 `feat/logistics-t07-tools`。复用T04指标和T06真实运行，未修改既有指标规则、源任务、正式账号或本机配置。

## 工具契约

核心注册表和分发器在 `project/tools/logistics.py`，宿主执行与授权在 `backend/logistics_tools.py`。六个工具提供标准函数声明、JSON输入结构、公共及逐工具输出结构。核心不导入后端/FastAPI、不读取环境变量、不创建连接。

| 工具 | 输入参数 | 主要结果 |
| --- | --- | --- |
| `get_snapshot_summary` | H/W，可省略 | `data.metrics`中的三指标及近期接单/完成；可见、完成及窗口缺失数 |
| `get_risk_distribution` | H/W、dimension、sort、分页 | `rows`中的快递员/AOI风险数量、下界、占比和完整快照分母 |
| `get_pending_tasks` | H/W、risk_state、sort、可选courier_id/aoi_id、分页 | 待揽收任务、等待/逾期/剩余分钟、风险状态及任务证据ID |
| `get_courier_workload` | H/W、sort、可选courier_id、分页 | 快递员可见/完成数量、积压与近期事件；风险占比独立放在risk_distribution |
| `get_quality_summary` | 空对象 | 当前可见字段的可用/不可用数量、适用分母、不适用数量及可用比例 |
| `get_evidence` | 必填evidence_id | 当前run快照的指定任务掩码记录及可计算的当前风险字段 |

H/W参数名为 `horizon_minutes` / `recent_minutes`，当前白名单仅接受整数30/60；与已验证 `logistics-metrics-v1` 一致。客户端不能临时改变指标口径；未来放开阈值须引入对应版本并独立验证。所有工具拒绝未知字段、布尔值冒充整数、错误类型、空标识及越界分页。`page`为1—100000，`page_size`为1—100，默认1/20。

- 风险类型：`all/overdue/due_soon/normal/window_unavailable`。
- 风险分布维度：`courier_id/aoi_id`；排序：`identity_asc/backlog_desc/overdue_desc/due_soon_desc`。
- 待揽收排序：`risk_priority/wait_minutes_desc/overdue_minutes_desc/remaining_minutes_asc`。
- 快递员负荷排序：`identity_asc/backlog_desc`。完成任务也计入当前可见及近期事件，因此可能返回积压为0的快递员。
- 身份过滤仅缩小已绑定快照中的结果，不改变授权范围；字符串标识保留前导零。未知对象返回empty，不代表不存在生产风险。

所有输出包括 `tool/status/run_id/snapshot_id/business_as_of/metric_version/data/rows/total_rows/returned_rows/truncated/limitations`，以及固定快照指纹、可见内容摘要、城市和区域。计数单位为task、时长为minute、占比为ratio，沿用T04。

## 调用方式

新增两个正式接口：

| 接口 | 用途 |
| --- | --- |
| `GET /api/logistics/tools` | 登录且具有读取角色后获取六个工具声明与输入/输出结构 |
| `POST /api/logistics/runs/{run_id}/tools/{name}` | JSON直接作为工具参数，服务器按已保存run绑定快照 |

示例：

```http
POST /api/logistics/runs/LRUN-已有运行/tools/get_pending_tasks
Content-Type: application/json

{"risk_state":"overdue","sort":"overdue_minutes_desc","page":1,"page_size":20}
```

模型调用路径可以由宿主绑定：

```python
tools = app.state.logistics_tools.for_run(authenticated_user, saved_run_id)
declarations = tools.declarations()
result = tools.call("get_pending_tasks", {"risk_state": "overdue", "page_size": 20})
```

`authenticated_user`、`saved_run_id`由后端提供；不能采用模型声称的账号或run。核心每次校验参数，宿主执行入口再次校验。无论HTTP、服务直调或缓存的工具分发器，每次都从数据库核对运行工作空间和ready快照，复核账号存在、成员启用、批次及当前授权区域。审核人可只读调用其范围内的运行工具；管理员无业务读取权时拒绝。没有开放任意SQL或原始源预览。

run、snapshot、城市、区域、批次和业务时点均不属于模型参数。未知工具及非法参数返回HTTP400；越权403、账号未登录401、不存在404、快照未ready或批次改变409。授权失败不能降级成空数据。

## 分页、状态和质量

汇总和质量工具不返回全量明细，`total_rows`为可见任务总数、`returned_rows=0`、`truncated=false`，实际聚合在data中。任务/分组/负荷工具先计算完整快照和全量筛选结果，再排序、分页；`total_rows`为筛选结果总数，不是当前页大小。风险分母始终为完整快照同类风险数量，不随筛选、排序或分页变化；快递员自己的指标分母与全快照风险分母分别输出。

同值任务以城市、区域、订单字符串、批次、来源行排序；默认风险优先级沿用T04。分组以身份字符串打破同值排序；缺失数值排在已知值之后，不把缺失当0。超出末页返回empty，但仍保留真实总数和分母。`truncated`表示当前页之后尚有明细，分页字段表明之前页的偏移。

| 状态 | 含义 |
| --- | --- |
| ok | 当前可见数据可计算；可计算0与缺失不同 |
| empty | 快照为空，或筛选/当前页没有返回行；不能证明生产零风险 |
| unavailable | 完整指标因窗口缺失不可计算，字段质量不足，或快照口径版本不支持 |
| error | 工具内部计算/存储异常；返回安全错误码，未知总数为null，禁止伪装成0 |

窗口缺失沿用T04：value为null、known_lower_bound为已知下界，占比不可计算时为null。工具内部异常不返回异常原文、连接配置或源字段；权限与参数错误仍按HTTP状态拒绝。

质量工具仅统计 `visible_quality`中的窗口、静态坐标、接单GPS和已发生完成事件的GPS。待完成任务的完成GPS为not_applicable；没有适用任务时比例为null。原始缺失和未来GPS都表现为“截至T无可用采样”，工具不能猜测具体原因。质量统计不读离线全量质量报告，不携带未来完成标签、最终时长或未来统计。

## 任务证据边界

任务清单返回 `LTASK:{run_id}:{snapshot_row_index}` 作为当前run的快照任务证据标识。展开时必须匹配当前run，查询使用已绑定snapshot_id和参数化行索引，不能通过替换证据ID访问其他run。读取仍复核实时快照权限；单凭证据ID不授权。

证据取自已持久化T03掩码行，可在服务重建后展开；待完成记录不含最终完成时间/日期/时长/超时标签，已发生完成事件允许显示。当前ID是快照任务定位，不是“已调用、已审计、已证明结论”的凭据。T08另行建设调用证据实体、摘要/耗时/结果指纹、引用规则和报告；本次不提前增加完整证据表或模型循环。

## 实际验证

在Scope根目录运行（完整回归关闭 `SCOPE_DATABASE_INTEGRATION`）：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest backend.test_logistics_tools project.tests.test_logistics_tools -v
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s backend -t . -p 'test_*.py' -v
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s project/tests -t . -p 'test_*.py' -v
.\.venv\Scripts\python.exe -X utf8 -m backend.eval_supply_agent --output test-results/logistics-t07/offline-eval.json
.\.venv\Scripts\python.exe -X utf8 scripts/verify-logistics-tools.py
.\.venv\Scripts\python.exe -m py_compile project/tools/logistics.py backend/logistics_tools.py backend/test_logistics_tools.py project/tests/test_logistics_tools.py scripts/verify-logistics-tools.py backend/main.py
git diff --check
```

真实核验脚本默认输出文件通过独占创建防覆盖，重跑需传入新的 `--output test-results/logistics-t07/另一个文件.json`。

- 初始专项9项全部通过；增加质量可用/未知口径及宿主直调检查后，最终8项后端+2项核心专项在完整回归中全部通过。
- 后端103项：90通过、13跳过；核心91项：73通过、18跳过；无失败。离线协议24/24；编译与空白检查通过（Git仅提示已有文件换行转换）。
- HTTP与直接调用验证：覆盖六工具参数越权、区域缩权、跨空间run/快照、成员停用、删除账号、管理员无读取权、批次改变、失败快照、跨run证据、SQL表达式拒绝。
- A—H夹具与标准库独立参考核对汇总、任务顺序、两维分组和完整分母；覆盖缺失、零值、empty、unavailable、error、末页、同快照证据及未来数据变化。修改未来完成/GPS/质量标签和新增未来任务后，六工具业务结果相同、JSON及日志无未来秘密标记。
- 真实源库只读、临时SQLite账号与run：区域5、`2000-07-02 10:00:00`的可见任务33506、积压627、逾期2、临期0、近期接单188、近期完成166，全部与独立复算一致。前20任务顺序、两维前20分组与分母一致，快递员负荷、质量适用分母、任务证据展开通过；修改范围400、撤权403。源批次存储摘要前后一致，没有执行源表写入，也未重跑全源内容哈希。

证据：[后端回归](../../test-results/logistics-t07/backend.log)、[核心回归](../../test-results/logistics-t07/project.log)、[离线协议](../../test-results/logistics-t07/offline-eval.json)、[真实源工具核验](../../test-results/logistics-t07/real-tools.json)、[结果汇总及实现摘要](../../test-results/logistics-t07/summary.json)。产物只在本地忽略目录保留，可按命令重新生成。

## 限制与下一步

本任务完成T07；T08—T24未推进，G2待T08/T09。T06确定性流程未改成模型调查，不自动消费这些工具；后续模型循环使用已提供分发器。工具调用审计、模型工具预算预留、报告、SSE和前端接线分别按后续任务实现，当前只读HTTP调用不冒充Agent预算轨迹。全量快照复算保持正确口径，尚未进行全市容量或多worker压测。

未运行真实模型、旧供应链PostgreSQL场景、前端构建或浏览器（本次无前端变更）。新增接口随现有启动入口加载，已有后端需重启；不新增依赖、不覆盖.env、不重新导入源任务。
