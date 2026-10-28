# T02：物流快照契约

版本：`logistics-snapshot-v1`。制定日期：2026-10-07（北京时间）。状态：T03 [快照与字段掩码](t03-snapshots.md)、T04 [指标与手算验证](t04-metrics.md)、T05 [当前入口 G1 验收](t05-g1.md) 已完成。尚未存在的模型工具、报告/导出和恢复入口在后续阶段继续验证。本契约与 [数据契约](data-contract.md)、[评测设计](eval-design.md) 配套。

## 1. 固定范围与时间

- 输入为已认证用户、工作空间、已保存数据批次、城市及区域集合、业务时刻 T。城市首版仅 Shanghai，先用区域字符串 `5`。区域是 `(city, region_id)`，不能套用 A/B 工厂权限。
- T 使用严格参考钟表格式 `2000-MM-DD HH:mm:ss`，不带时区偏移，不隐式转换 UTC，不接受缺少年份、非法日期或真实年份替代。`reference_year=2000`、`actual_year=null`、`year_status=undisclosed`；界面标“历史回放／参考时间”。Asia/Shanghai 仅为展示约定。
- T 表示该时刻所有事件处理完毕。接单、完成均使用 `<= T`；演示 T1 为 `2000-07-02 10:00:00`，T2 为 `2000-07-02 10:30:00`。
- H=30 分钟、W=60 分钟为项目设置，不是行业规范。近期事件为 `(T-W,T]`。创建后不能修改 T、批次或区域；改变条件创建新快照。
- 系统操作时间 `recorded_at/created_at` 是当前真实带时区时间，与 `business_as_of` 分开。禁止用真实时钟悄悄推进回放。

## 2. 候选集合与计算顺序

1. 后端验证用户启用、空间成员资格、该空间选择的批次和明确物流区域授权。请求区域必须全部获授权，否则整体拒绝，不静默缩小范围。没有物流授权记录时默认拒绝，现有连接管理权限不自动转换成区域读权限。
2. Repository 从核验过的批次读源记录，按严格格式派生参考时间。源表不新增或回写参考时间；保留原文供授权管理核查。
3. 以 `batch_id + city + 授权 region_id` 限定来源，再按 `accept_time_reference <= T` 选择任务。不得按 `ds`、完成日期、`eligible_duration/eligible_sla/eligible_lifecycle` 或事后质量标签过滤候选。跨日和期初积压必须保留，不只读当天接单记录。
4. 在候选集合中，仅 `pickup_time_reference <= T` 的完成事件可见。其余视为“截至 T 未观察到完成”，不是“任务永远未完成”。此数据是已完成任务的历史样本，积压不保证代表真实生产全量未完成或取消任务。
5. 可见字段逐项按下面白名单构造新对象，禁止先返回源行再让前端删除列。白名单之外一律不返回。
6. 按可见字段重新计算当前质量和指标。GPS 缺失不能使时效任务被剔除；窗口无效不能当作没有逾期。

编号缺失/冲突、核心日期不可解析或完成早于接单等违反批次前提时应给出明确数据错误，不能猜年份、补一天或静默去重。对窗口缺失的隔离夹具：积压仍可计，逾期/临期标不可计算并返回缺失数；不得把 null 当 0。源库本批核心日期合法不代表未来批次自动合法。非法格式是数据错误专项，不属于“保持合法事件顺序、只改 T 后事件”的未来不变性输入域。

## 3. 快照元数据白名单

| 字段 | 约束 |
| --- | --- |
| `snapshot_id`、`workspace_id` | 服务端生成/绑定；快照 ID 不是授权凭据 |
| `dataset`、`city`、`region_ids` | `LaDe-P`、Shanghai、排序去重的已授权字符串区域 |
| `batch_id`、`data_version` | 绑定核验批次；data_version 包含源文件及导入内容摘要，禁止运行中自动切换最新批次 |
| `business_as_of`、`reference_year`、`actual_year`、`year_status` | 明确参考轴，真实年份 null |
| `contract_version`、`metric_version` | 分别绑定本契约和后续确定性指标实现版本 |
| `permission_version` | 记录创建时的授权版本；读取仍复核实时权限 |
| `status`、`created_at` | building/ready/failed；只有 ready 可进入工具与调查 |
| `visible_content_hash` | 只对规范化后的可见业务行计算 SHA-256；按稳定任务键排序 |
| `snapshot_fingerprint` | 绑定上述数据版本、范围、T、口径及可见内容；不将其误作业务可见内容的单独哈希 |
| `coverage`、`limitations` | 样本内范围、缺失与已完成样本限制；禁止携带全库事后质量计数 |

同批次、范围、时刻和契约的掩码内容可以复用；授权版本改变必须重新检查，缓存命中不免检。不同工作空间即使内容相同也分别授权，不能把其他空间快照 ID 直接复用。全量源数据指纹为宿主溯源元数据，不必进入模型正文；不根据指纹变化推断业务原因。

## 4. 行级字段白名单

下表是允许输出的最大集合。模型工具默认只返回问题需要的投影和分页，不能据此一次输出全库。

| 输出字段 | 来源 | 可见条件及语义 |
| --- | --- | --- |
| `task_key` | `(dataset, city, order_id)` | 已接单；用结构化对象，不把 ID 转成数字 |
| `batch_id/source_file/source_row` | 原记录定位 | 已接单；source_file 只输出逻辑相对名称，不输出本机绝对路径 |
| `order_id/city/region_id/courier_id/aoi_id/aoi_type` | 同名原始字段 | 已接单；静态属性假设接单时已知，AOI 类型不翻译成未知业务名称 |
| `accept_time/accept_time_reference` | 接单原文/严格参考时间 | 已接单；不是平台下单时间 |
| `time_window_start/time_window_end` 及对应 `_reference` | 要求窗口 | 已接单；未来截止时间是已知承诺，可见，不是未来实际完成事件 |
| `lng/lat` | 任务停靠点 | 已接单，成对、数值合法；仅粗粒度辅助，坐标系未认证 |
| `completion_observed` | 程序判断 | true 仅当完成时间 <=T；false 表示截至 T 未观察到完成 |
| `pickup_time/pickup_time_reference/ds` | 完成原文、参考时间、MMDD | 仅完成已发生；未发生时这些键完全省略，不返回原值或最终标签 |
| `accept_gps_time/accept_gps_time_reference/accept_gps_lng/accept_gps_lat` | 接单附近 GPS | 接单已发生且采样 <=T；时间与坐标三元组全部有效才成组输出，否则全组省略 |
| `pickup_gps_time/pickup_gps_time_reference/pickup_gps_lng/pickup_gps_lat` | 完成附近 GPS | 完成已发生且采样 <=T；同样整体判断；即使 GPS <=T，完成未发生也不能放行 |
| `current_wait_minutes/current_overdue_minutes/risk_state/remaining_minutes` | 可见事件及窗口重算 | 待揽收才有当前等待/逾期；窗口不可计算时相关值 null，并说明口径限制 |
| `completed_duration_minutes/completed_late_minutes` | 已观察完成事件重算 | 仅已完成且相应字段有效，不复制源表事后派生值 |
| `visible_quality` | 当前字段重算 | 仅接单身份、窗口、已放行坐标等质量；GPS 不可见统一记“截至 T 无可用采样”，不区分原始缺失与未来采样 |

静态属性、窗口没有业务版本历史，只能采用“接单时已知”的回放假设；不能宣称已还原生产系统真实信息到达时间。匿名 courier_id 不对应系统账号或联系人。

## 5. 源派生列与禁止内容

以下 15 个源派生字段均不原样进入快照：

| 源列 | 处理 |
| --- | --- |
| `eligible_duration/eligible_sla/eligible_lifecycle` | 包含最终完成知识，不能过滤待揽收；当前计算资格重新判断 |
| `eligible_stop_spatial/eligible_accept_spatial/eligible_pickup_spatial` | 不直接复制；按当前可见坐标及采样门禁重算 |
| `accept_gps_distance_km/pickup_gps_distance_km` | 首版不输出；未来需要时只从已放行坐标重算并标近似距离 |
| `accept_spatial_review_required/pickup_spatial_review_required` | 不直接复制；任何时间偏差/距离诊断都必须在采样放行后才计算 |
| `quality_flags` | 整个原数组禁止；包含跨日、最终超时、最终时长和未来 GPS 判定，不能仅删除一两个标签 |
| `duration_minutes/late_minutes/is_late` | 原值禁止；已完成时按可见事件另算，待揽收绝不返回 |
| `year_status` | 来自已校验批次元数据统一给出，不信任单行自由字符串 |

同样禁止：原行 JSON、任意 SQL 结果、批次完整 audit_summary、全库质量规则命中数、未来完成日期分组、最终是否超时的统计、含未来字段的日志/导出/异常正文、其他空间证据。Agent 不接收 `.env`、DSN、原始数据库连接或源表查询工具。评测答案与冻结评价源行清单不进入运行上下文。

## 6. 指标与稳定排序契约

定义 V 为已接单可见任务，C 为其中已观察完成的任务，B=V\C。

- 样本内积压：`|B|`。
- 当前逾期未完成：B 中 `window_end<T`，严格小于；当前逾期分钟 `(T-window_end)/60秒`。
- 临期未完成：B 中 `T<=window_end<=T+H`，含两个端点；不能把等于 T 的截止当已逾期。
- 当前等待：B 中 `(T-accept_time)/60秒`。保留精确分钟，小数不用于提前取整后分类。
- 近期接单/完成：各事件位于 `(T-W,T]`；计数范围必须相同，完成含期初积压，不能只统计近期接单者的完成。
- 排序：逾期 → 临期 → 普通 → 窗口不可计算；逾期按逾期分钟降序，临期按剩余分钟升序，普通按窗口终点升序；同分按 `(city, region_id, order_id, batch_id, source_row)` 稳定排序，ID 用字符串序。
- 风险分布分母分别为全授权范围的同类风险总数，不能把分页返回数当总数；快递员/AOI 集中不等于责任或因果。
- 若 B 里有窗口未知任务，已知逾期/临期数可作为下界单列，但完整指标标不可计算；零值仅在对应集合完整且可计算时使用。没有可见任务显示 empty/覆盖限制，不宣称整个区域无风险。

工具返回 `snapshot_id/fingerprint/metric_version/denominator/unit/total_rows/returned_rows/truncated/status`，状态区分 ok、empty、unavailable、error。工具只能用服务端绑定的 snapshot_id，不能从模型参数接受新的批次、T 或区域覆盖值。

## 7. 权限与后续实现门禁

T03 前必须配置仅供后端 Repository 使用的最小权限源读取账号；具体当前差距见 [数据契约](data-contract.md)。物流首版只开放 T07 的参数化工具，不为已有供应链 SQL 工具增加 `lade` 白名单。`project/` 不直接读取数据库或配置。

所有创建、读取、工具、证据、报告下载和恢复入口复核空间、批次、区域及实时用户状态。撤权后不可用旧缓存或检查点继续交付。创建版本、当前权限和报告证据范围不一致时拒绝；仅修改前端按钮不可算验收。

## 8. T03—T05 验收清单（尚未运行）

1. V1 A—H 夹具 T1：可见 7、完成 2、积压 5、逾期 2、临期 2；顺序 G→A→H→B→C。T2：积压 4、逾期 2、临期 1，必须含新接单 E。
2. 接单等于 T、完成等于 T、窗口等于 T、窗口等于 T+H、事件等于 T-W 四类边界；零时长和跨日分别断言。
3. 只改合法的 T 后完成时间、完成 GPS、接单未来 GPS及相应派生标签，固定可见成员：可见行、指标、排序、模型输入、下载正文及 visible_content_hash 不变。真实数据版本摘要变化可以改变溯源 fingerprint，不能伪装成业务输出变化。
4. 添加 T 后才接单任务，T 时点业务输出不变；改变未来 ds/跨日标签不能使该任务进入候选。
5. 完成已发生但 GPS 采样晚于 T；采样 <=T 但完成晚于 T；原始缺失与未来采样均无泄漏。
6. 跨空间 ID、未授权区域、混合授权区域、失效账号、创建后撤权及伪造 tool 参数均拒绝；绕过浏览器直接请求同样有效。
7. 精确 JSON 键、嵌套证据、日志、导出及上下文检查，不只断言 UI 不显示。质量字段、分页排序也要做未来不变性。
8. 三个实际时点独立复算，至少一处跨日积压；同一范围下核对 `B(T2)=B(T1)+接单(T1,T2]-完成(T1,T2]`。独立脚本不得导入被测快照/指标函数。

这些是 G1 的验收要求，T02 仅确定契约，不将它们标为已经通过。
