# T11：持久化 LangGraph 调查循环

## 使用方式

默认工作台仍启动确定性调查。已配置模型服务且有快照调查权限时，可显式创建模型运行：

```http
POST /api/logistics/runs
Content-Type: application/json

{"snapshot_id":"已保存快照ID","idempotency_key":"唯一请求键","mode":"model","question":"定位当前积压最多的快递员，说明样本局限"}
```

创建先落盘再返回202；未配置模型返回503，同幂等键不同模式或问题返回409。区域、业务时点和快照范围由宿主绑定，模型不能覆盖。使用`GET /api/logistics/runs/{run_id}/investigation`读取保存的计划、步骤状态、观察、工具轨迹和模型报告；当前授权及证据指纹重新校验。`/runs`分别展示模型状态与确定性模板报告，普通工作台未新增模型启动按钮。

## 调查与持久化

图为上下文、规划、选择校验、执行工具并保存证据、评估观察、决定继续、报告和收尾。首次工具由模型按目标选择；完成步骤后携带真实证据和观察修订一次计划。每条轨迹关联计划版本、步骤ID、call_id、evidence_id及来源。工具来自T07参数化白名单，数量和排序沿用T04，模型不执行自由SQL。

提交报告前必须取得汇总、重点任务和质量证据。缺少时由宿主补取，标记`origin=report_requirement`；这些步骤的观察标记`source=program_requirement_check`，只记录实际状态。模型选择的步骤仍生成真实模型观察。模型报告经过T10结构、引用与计算值校验，保存在图状态和节点日志；T08模板报告复用同一保存结果，不重复调用工具。模型上下文最多投影三行并标明截断，完整证据保存不变、校验仍使用完整证据。自然语言推断的业务正确性仍需人工核查。

业务SQLite幂等增加run的mode/question/thread_id及`logistics_graph_operations`。检查点位于业务库旁的`logistics-graph.sqlite`，稳定线程ID为`logistics:{run_id}`，节点同步落盘。操作日志有内容指纹；检查点落后时复用已完成节点，避免重复模型计费和工具执行。崩溃遗留started节点因执行结果不确定转waiting/partial，不自动重发。备份需在停机后同时保存业务库、检查点库及相应SQLite文件；不能只复制其中一个数据库。

复用T06单进程、单运行执行器、租约/心跳/领取凭证，每节点和交付均重新校验权限和凭证；停机先结束线程再关闭图和业务连接。取消、撤权、预算耗尽和非法模型结果不能冒充成功报告。waiting保存stop_reason及manual_required；公开人工恢复留T16—T18。复杂反证、空转和多次修订策略留T12。

## 验证与真实案例

`backend/test_logistics_workflow.py`七项使用真实LangGraph和SQLite、模拟HTTP模型；涵盖结果进入决策、独立子进程恢复计划、inflight不重复计费、检查点落后复用工具、非法输出、撤权/取消、幂等及预算终止。模板报告ID也校验写入图状态和运行结果。

真实调用脚本为`scripts/verify-logistics-loop.py --live`，`--case 2`可只复查第二案例。使用独立手算开发夹具和临时SQLite，不读取正式源任务或冻结评测。服务为deepseek-chat，输出模式tool_call，Token上限24000未放宽。

| 案例及产物 | 结果 | 耗时 | 模型调用 / 已知Token | 实际决策 |
| --- | --- | --- | --- | --- |
| 快递员积压，`live-cases-verified.json`案例1 | completed，模型报告partial | 42.188秒 | 5 / 22556 | 首选风险分布，再按观察查具体快递员负荷，完成引用报告 |
| AOI逾期，同文件案例2 | waiting，plan_version | 19.109秒 | 4 / 14377 | 首选汇总，保存真实观察；错误修订版本被拒绝 |
| AOI逾期，`live-case2-version.json`复查 | waiting，model_budget_exhausted | 44.750秒 | 6 / 24305 | 明确版本提示后完成修订和分布查询，报告生成触及预算被拒绝 |

前期`live-cases.json`、`live-cases-final.json`、`live-cases-compact.json`和`live-cases-bounded.json`保留原始失败记录，分别涉及上下文Token过多或非法工具参数。去除重复协议、投影证据并避免对报告必需补取重复调用模型后，一个案例完整完成。第二案例仍存在预算限制，不能宣称两个案例均完整成功。实际返回用量可能超过请求前剩余额度，返回后拒绝交付并记账；不能把24000当成供应商账单硬上限。

T11通过标准已验证：模型实际选择对象/工具，至少一次真实工具结果进入下一轮决策，证据来自程序执行。完整两案例报告成功率和因果结论质量未通过验收，G3及T12—T24保持未开始。最终回归计数与命令见[任务状态](task-status.md)及本地`test-results/logistics-t11/summary.json`。
