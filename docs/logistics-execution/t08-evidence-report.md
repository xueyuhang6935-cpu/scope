# T08：证据记录与确定性报告

完成日期：2026-10-08（北京时间）。分支 `feat/logistics-t08-evidence`，实现版本 `T08-20261008`。保留所有已有未提交修改，未写源任务或正式账号，未修改本机配置。

## 持久化调用与证据

新增幂等迁移表：

| 表 | 内容 |
| --- | --- |
| `logistics_tool_calls` | 调用ID、run/空间/快照/口径、工具、参数摘要、开始/结束、执行状态、耗时、证据ID、结果指纹及安全错误码 |
| `logistics_evidence` | 独立证据ID、run/空间/快照/口径、快照指纹、来源调用、来源类型、内容指纹及不可变JSON |
| `logistics_reports` | 每个run唯一基础报告、结构化JSON、内容指纹及创建时间 |

复用T07注册、校验、计算和T06 SQLite事务。旧供应链审计的业务scope及结果格式不同，因此保留原模块，物流不向旧表写不同协议。

工具执行前单独落盘started；结果证据、任务证据及审计终态在同一事务保存。调用状态沿用 `ok/empty/unavailable/error`，进程中断的started在应用下次启动标记 `interrupted`，不编造结果或耗时。证据保存失败回滚全部新证据，并拒绝交付未持久化结果；调用仍记录安全失败码。未通过参数/初始权限门禁的请求不是已启动工具执行，不保存其原始输入；合法调用中的证据查询拒绝会记为error。

参数摘要只保留白名单枚举/整数，快递员、AOI和输入证据标识保存SHA-256摘要及长度。环境配置、密码、Cookie、连接字符串、异常原文、非法SQL输入不进入审计。此处摘要不是身份匿名化承诺，也不代表业务结果没有任务标识。

工具响应增加 `call_id/result_evidence_id/result_fingerprint`。每次执行的完整业务结果保存为 `LEV-*`，指纹按不含新增调用元数据的规范JSON计算。待揽收行中的 `LTASK:{run_id}:{row_index}` 同时保存为快照任务证据；重复相同内容保留首次来源调用，内容冲突拒绝覆盖。重建服务或重启应用后仍可读取。

`get_evidence`继续支持原T07任务定位，新增LEV工具结果展开。T07任务定位首次直接展开也会持久化为证据。任务证据和工具结果证据来源分别为 `snapshot_task/tool_result`，不能把错误工具结果或有效ID当成已证明的结论。

## 正式接口

所有接口使用已有认证和物流范围校验；下载链接不使用本地文件路径。

| 接口 | 行为 |
| --- | --- |
| `GET /api/logistics/runs/{run_id}/calls` | 审计列表，严格分页，默认1/20，page_size上限100 |
| `GET /api/logistics/runs/{run_id}/evidence/{evidence_id}` | 读取该调查的已保存证据及来源、指纹，不增加新工具调用 |
| `POST /api/logistics/runs/{run_id}/report` | 空JSON，发起人且仍有调查区域权可生成基础报告，重复请求返回原报告 |
| `GET /api/logistics/runs/{run_id}/report` | 已保存结构化报告JSON，不重新计算 |
| `GET /api/logistics/runs/{run_id}/report/view` | 从同一JSON渲染无脚本HTML报告页面 |
| `GET /api/logistics/runs/{run_id}/report/markdown` | 从同一JSON渲染Markdown，attachment下载及nosniff |

查询不能携带快照、范围、时点或文件路径覆盖参数。每次读取证据、报告、页面和下载，重新核对账号存在、启用成员、run工作空间、ready快照、已保存批次及当前区域权；证据同时必须属于当前run/空间/快照并匹配快照指纹。跨run引用直接拒绝，即使两run使用同一快照也不凭ID授权。

审核人可只读查看授权范围内的报告和证据，不能生成别人的报告。系统管理员无业务读取权时拒绝。取消/失败run不能生成新报告，已保存的历史报告仍可在当前读取权限下查看。参数400、权限403、未登录401、不存在404、内容校验或数据条件冲突409；没有以零数据掩盖错误。

## 报告内容与生成

报告生成显式调用已绑定run的汇总、第一页前20条待揽收任务、当前可见质量三个工具并记录审计。工具失败或未知口径不能生成报告；empty和窗口缺失允许生成，但必须保留对应状态、null及已知下界。后续模型调查可复用这些证据接口；本次不改动T06确定性执行节点，也不伪造模型自主调查。

核心纯函数位于 `project/agents/logistics_report.py`，只接收已授权、同run/快照/时点/口径的工具结果。基础报告保存：

- 城市、区域、参考业务时点、快照与内容指纹、指标版本及H/W。
- 三指标、近期接单/完成、可见与完成数量、窗口缺失及汇总证据。
- 按T04风险优先级的前20条重点任务、等待/逾期/剩余分钟、快递员/AOI和逐任务证据。
- 任务总数、实际展示数和截断标记，质量可用/不可用/适用/不适用数量及质量证据。
- 样本范围、参考年份、GPS与字段限制，以及按已知风险/缺失生成的待人工核查事项。

固定规则只提出人工核查、缺失补充和新快照复核，不生成未经证据支持的因果判断，不创建真实派单，也不将历史完成归因于建议。数量来自工具计算，不由语言模型编写。

结构化报告先入库，再经统一授权和引用检查交付。报告重读会校验JSON指纹、三个结果证据、逐任务证据，并从已保存工具结果重建比较业务字段；即使只修改数字并重算报告哈希，仍会因证据不一致拒绝下载。HTML和Markdown都从同一个已保存对象渲染，不读源库或重新计算数字。

HTML转义源标识、不执行脚本，使用限制性CSP；Markdown转义HTML和链接/表格控制符。手机宽任务表使用容器横向滚动，避免压碎数字。页面目前为受控的基础报告视图，原React运行列表和SSE接线仍属于T09。

## 实际验证与证据

在Scope目录执行：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest backend.test_logistics_report project.tests.test_logistics_report -v
.\.venv\Scripts\python.exe -X utf8 -m unittest backend.test_logistics_tools project.tests.test_logistics_tools -q
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s backend -t . -p 'test_*.py' -v
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s project/tests -t . -p 'test_*.py' -v
.\.venv\Scripts\python.exe -X utf8 -m backend.eval_supply_agent --output test-results/logistics-t08/offline-eval.json
.\.venv\Scripts\python.exe -X utf8 scripts/verify-logistics-tools.py --report --output test-results/logistics-t08/final-report.json
git diff --check
```

完整回归关闭 `SCOPE_DATABASE_INTEGRATION`；使用已有虚拟环境。新增及受影响实现/测试/脚本均执行Python编译检查。核验脚本主JSON通过独占创建，重跑必须换新的输出名称，附带报告JSON/Markdown/HTML只作为本地验收产物。

- T08专项10项（8后端+2核心）通过；调整手机表格后相同10项再次通过。原T07专项10项通过，更新比较方式仅排除新增的逐次调用元数据，没有放松业务范围或未来不变性断言。
- 完整后端111项：98通过、13跳过；完整核心93项：75通过、18跳过；无失败。完整回归之后仅调整报告表格CSS/类名与验证脚本导出结构化报告，受影响T08专项及真实核验再跑通过。离线协议24/24、编译及空白检查通过。
- A—H夹具报告五指标及任务顺序与独立参考一致；所有重点任务可读证据。重复报告不新增调用或报告；模拟保存失败无孤立证据；模拟异常不保存秘密原文。
- 验证跨run、证据空间/快照错配、区域缩权、停用、删除账号、批次改变及跨空间请求，阻断报告/HTML/下载/证据；新应用读取同磁盘报告通过，未完成审计标interrupted；内容篡改及报告数字与证据不一致均拒绝。
- 修改未来完成/GPS/最终标签后，报告数字、质量、限制和任务内容不变；证据JSON、报告JSON、Markdown和HTML无未来秘密标记，待完成重点任务无最终完成字段。覆盖空样本、可计算0、窗口缺失、工具失败及25条任务仅展示20条。
- 真实源库仅只读，临时SQLite账号与运行：区域5参考10:00积压627、逾期2、临期0、近期接单188、近期完成166，与独立复算一致；报告20条任务及汇总/质量/清单证据全部展开，页面/下载/JSON一致，重复生成幂等，撤权后证据及下载403。源批次存储摘要不变；未重新计算全源内容哈希。
- Playwright对真实生成HTML分别检查1440×1000及390×844视口、三个表格和页面宽度；查看最终桌面/手机截图，任务表内部滚动、正文无横向溢出。

证据：[完整后端](../../test-results/logistics-t08/backend.log)、[完整核心](../../test-results/logistics-t08/project.log)、[离线协议](../../test-results/logistics-t08/offline-eval.json)、[最终真实核验](../../test-results/logistics-t08/final-report.json)、[结构化报告](../../test-results/logistics-t08/final-report.report.json)、[Markdown](../../test-results/logistics-t08/final-report.md)、[HTML](../../test-results/logistics-t08/final-report.html)、[视觉检查](../../test-results/logistics-t08/visual-qa.json)、[桌面截图](../../test-results/logistics-t08/desktop.png)、[手机截图](../../test-results/logistics-t08/mobile.png)、[结果汇总](../../test-results/logistics-t08/summary.json)。产物保存在本地忽略目录，命令可重跑。

## 阶段边界

T08完成，G2仍待T09事件流及真实运行页面。报告生成当前由显式接口触发，未接模型规划/自动报告节点/前端运行列表，未执行审批或派单。不新增依赖，未改前端源码；未运行完整既有React浏览器套件/前端构建、真实模型、旧供应链PostgreSQL场景或全市容量压测。已有后端需重启加载表和接口。

证据JSON是应用存储快照，不是外部防篡改签名；原数据访问权限始终由后端裁定。首版单进程及全量Python复算边界沿用T06/T07，多worker/外部存储和备份演练属于后续稳定性任务。
