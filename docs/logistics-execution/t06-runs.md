# T06：真实运行与持久化执行器

完成日期：2026-10-08（北京时间）。分支：`feat/logistics-t06-runs`。实现版本：`T06-20261008`；沿用原有未提交变更，没有重新导入或修改源任务、账号、区域授权及本机配置。

## 接口与使用

正式入口已经挂载以下接口；启动时由 FastAPI lifespan 启动执行器，停机先停止两个线程，再关闭数据库。重启已有后端即可加载新增表和接口；继续维持单应用进程。

| 接口 | 参数与结果 |
| --- | --- |
| `POST /api/logistics/runs` | JSON 包含 `snapshot_id`、`idempotency_key`，可选 `parent_run_id`；先事务落盘，返回 **202** 及真实 `run_id` |
| `GET /api/logistics/runs/{run_id}` | 查询状态、已保存节点、预算、主动秒数、时间、心跳、租约截止、结果和安全错误码；不返回领取凭证 |
| `POST /api/logistics/runs/{run_id}/cancel` | 空 JSON；仅原发起人可取消非终态运行，重复取消保持终态，完成/失败不会改写 |

创建示例（使用已有登录会话和 ready 快照）：

```json
{"snapshot_id":"SNAP-已有快照标识","idempotency_key":"investigation-request-001"}
```

幂等键按工作空间和账号 ID 隔离，最长128字符；同键、同快照、同父运行重复请求返回原 run，异参数返回409。父运行必须存在于当前空间、当前账号可读且属于发起人。不接受客户端指定状态、预算、范围或业务时点。

读取与操作均核对当前空间、有效账号、启用成员、ready 快照、已保存批次及实时区域权限。只有调查员可创建；审核人可读取其授权范围内的运行，但不能创建或取消别人的运行。工作线程在每个节点前及提交结果前重新校验发起人的读取与调查权限，撤权后保存安全失败码、不保存新结果。

## 持久化与执行契约

新增 `logistics_runs` 表，外键关联快照和父运行，唯一键保障重复创建不增加任务。运行状态为 `queued/running/waiting/completed/failed/cancelled`。保存当前节点、预算计数与上限、主动时长、取消标记、创建/更新时间、租约、心跳、领取凭证及结果。

执行器有一个执行线程和一个心跳线程；数据库承担排队，不为每次 HTTP 请求分配 Future 或 BackgroundTasks。没有浏览器连接时也会执行。当前确定性安全节点为 `validate → finish → done`：检查固定快照和实时权限后保存快照标识及可见内容摘要，结果明确标记 `deterministic-runtime-check`。这只验证运行基础，不生成调查结论、建议或模型报告。

领取、续租、结果提交均使用 SQLite `BEGIN IMMEDIATE` 事务。每次领取生成新凭证，默认租约10秒、心跳1秒；提交必须匹配运行中的当前凭证且租约尚未过期。取消及终态清除凭证和租约，旧线程不能回填结果。启动后自动扫描 queued 和租约已到期的 running，按已保存节点继续；未到期租约不会被提前抢占，后台轮询会在到期后领取。

节点结果和下一节点一起提交。正常停止在当前安全节点落盘后释放为 queued；异常进程退出则由租约超时触发恢复。当前节点没有外部写入，可安全重执行；未来带外部副作用的节点必须额外满足幂等契约。

初始累计预算为模型12次、工具16次、计划修订3次、主动运行120秒。T06不调用模型或工具，前三类计数为0；主动秒数按心跳及状态提交累计，排队和 waiting 不计时，恢复不清零。失联段至多计入原租约截止，属于保守计时；不是供应商计费数据。心跳发现主动预算耗尽会失败并撤销凭证，迟到结果无法提交。父运行目前仅保存关联；家族累计预算属于T12。

waiting 是已实现并验证的持久化执行状态，当前公开流程不主动进入人工等待；测试注入确定性 waiting 节点验证不续租、不再次领取、可取消。人工反馈、审批及恢复接口仍属于T16—T18，不能用测试节点冒充已实现业务流程。

## 实际验证与证据

在 Scope 目录运行，关闭 `SCOPE_DATABASE_INTEGRATION`：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest backend.test_logistics_runs -v
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s backend -t . -p 'test_*.py' -v
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s project/tests -t . -p 'test_*.py' -v
.\.venv\Scripts\python.exe -X utf8 -m backend.eval_supply_agent
.\.venv\Scripts\python.exe -m py_compile backend/logistics_runs.py backend/test_logistics_runs.py backend/main.py
git diff --check
```

- 初次专项8项加被意外导入发现的既有快照6项，共14项通过；随后消除重复发现并新增生命周期及阻塞预算测试。最终10项T06专项全部包含在95项后端回归中通过，没有将早期14项误称T06专项。
- 后端95项：82通过、13跳过，无失败；核心89项：71通过、18跳过，无失败；离线协议评测24/24。编译及差异空白检查通过（Git仅提示已有文件换行转换）。
- 验证事务幂等、重复请求冲突、父运行、跨空间/角色/撤权拒绝、排队、完成、失败、waiting、运行中取消、心跳、预算耗尽和迟到提交阻断。
- 真正启动独立子进程，在已保存 `finish` 安全节点强制结束进程；确认未到期租约不能抢占，再显式推进测试库租约为已过期，启动另一独立进程，只执行磁盘保存的 `finish` 节点并完成。没有把同进程重建对象当作进程重启证据；为缩短测试，不等待自然10秒租约。
- FastAPI lifespan 重启后自动处理此前磁盘queued任务，退出确认执行/心跳线程先结束。阻塞第一个运行时，第二个持续queued；停止发出浏览器请求后第二个仍完成。正常关闭在安全节点保存queued，新执行器从下一节点完成。

证据：[汇总及源码摘要](../../test-results/logistics-t06-summary.json)、[后端完整输出](../../test-results/logistics-t06-backend.txt)、[核心完整输出](../../test-results/logistics-t06-project.txt)。产物在忽略目录，仅本地保留；可按上述命令重跑。测试全部使用隔离SQLite与确定性快照夹具，未写正式业务库。

## 阶段边界

T06完成；T07—T24未推进，G2尚未验收。前端发送/运行列表尚未接通，属于T09。未运行真实模型、真实源快照执行联调、前端构建/浏览器（本次无前端修改）、旧PostgreSQL场景集成、多worker/全市容量测试。后续模型/工具节点须有请求超时及取消边界，不能以本次有限确定性节点的停机检查证明长请求可强制终止。无需新增依赖、重建数据库或修改源表。
