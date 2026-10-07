# 聊天动作与调查衔接

## 用户操作

| 输入或按钮 | 实际行为 |
| --- | --- |
| 查询 P001 库存 | 按原查询能力读取授权记录 |
| 这个缺口怎么算的 | 解释原结果，显示依据结果时间及证据截止时点，不重新查询 |
| 现在还是缺吗／重新查询 | 创建新任务，读取当前配置并重新计算，保留旧结果 |
| 继续查为什么晚交 | 从已保存计算中定位风险，校验快照后启动或关联 LangGraph 调查 |
| 怎么解决、哪个方案更好 | 复用有效候选方案；没有方案则调查试算；数据变化导致方案失效时触发重规划 |
| 批准这个方案 | 显示关联方案的核对入口；需选定方案、填写意见、明确点击批准或驳回 |
| 立即下发调拨单 | 明确提示未接入 ERP/WMS 执行，不生成执行成功记录 |

每个查询结果也提供解释、刷新、调查、比较方案按钮，携带原任务编号。多风险结果必须选择工厂、物料、版本，不自动取第一条。没有缺料计算结果时提示先查询；范围或条件不支持时等待澄清／停止重提，不忽略条件执行。

## 数据与上下文

- 业务查询类型仍为 `inventory / quality / readiness`；动作独立保存在 `planning_json.action`，包括 `query / explain / refresh / investigate / simulate / approval_request / unsupported_execution`。
- `result_json` 继续保存不可变查询证据。解释结果记录 `basedOnTaskId`、`evidenceCapturedAt`、`evidenceSource`；刷新结果记录原任务及 `refreshNotice`。
- `result_json.workflow` 保存 `riskId`、`runId`、`basedOnTaskId`、`windowDays`、所选风险及可选 `planId`。对象均由后端从授权会话结果解析，模型不能创建或指定对象编号。
- `supply_threads.focus_task_id` 保存当前讨论对象。选择较早消息中的方案也会更新焦点，后续“批准这个方案”能够指向该方案；新查询或新调查推进焦点。旧数据库启动时自动添加此列。
- 解释和刷新可以读取同会话旧时点的依据，但解释保留原时点，刷新使用当前配置；普通查询原有的快照边界仍保留。解释不能悄悄改变原结果的工厂、物料或时间范围。
- 模拟场景窗口仍从固定起点开始。重新计算且 `asOf` 未变化时明确提示“数据截止时点未变化”，不声称接入实时数据。

## 调查复用与进度

聊天入口与风险详情共用 `SupplyWorkflow.start_investigation`。首次关联时校验原计算的场景、证据截止时点、快照摘要、窗口和具体物料版本；排队后再次核对数据。窗口以 `input.windowDays` 保存并传递给重规划，不修改全局监测窗口。

首次发起的聊天问题随运行保存为 `input.question`，传入调查报告与可选模型的调查上下文，作为本次取证关注点；问题文本不改变工具权限和审批边界。复用已有运行时不会伪装为重新执行了一轮模型调查。

仅在用户要求调查／试算时按需建立风险。局部风险合并复用监测逻辑，但不会将未选物料判为已覆盖。有效且范围一致的进行中调查或已批准方案可复用；不能借聊天绕过旧调查范围冲突、权限和模型预算限制。

新增 SQLite 表 `investigation_stages`，在真实图节点进入时记录 `collect / investigate / plans / approval / finish`。前端每两秒轮询运行记录，显示真实状态和最后进入的阶段；读取失败时停止显示旧报告并提供刷新。自动重规划通过后端子运行关联继续跟踪。

`simulate` 首版复用完整调查与试算流程，没有新建独立优化器。方案数量、到线时间、费用及剩余缺口继续由确定性程序计算，模型只补充解析和报告。规则分类覆盖明确常用表达，复杂表达仍依赖可选模型或用户澄清。

## 接口

| 接口 | 用途 |
| --- | --- |
| `POST /api/supply/threads/{id}/messages` | 保留原字段，支持可选 `basedOnTaskId`；执行动作由后端识别 |
| `POST /api/supply/tasks/{id}/select-risk` | 从本任务候选中选择 `factory / material / revision` |
| `POST /api/supply/tasks/{id}/select-plan` | 持久化 `planId`；须属于关联调查及其服务端重规划链 |
| `GET /api/supply/investigations/{id}` | 当前工作空间、权限校验后的运行阶段、报告、方案及下一轮编号 |
| `GET /api/supply/plans/{id}` | 复用原方案详情及失效校验 |
| `POST /api/supply/plans/{id}/decision` | 复用原版本、摘要、权限、额度、意见与禁止自批校验 |

审批仍由另一位有权限的用户执行。方案选择不是批准；自然语言批准不是审批决定；审批决定不是 ERP/WMS 执行。授权、预算、自动重规划轮次限制保持原有实现。

## 验证入口

```powershell
.\.venv\Scripts\python.exe -m unittest backend.test_supply
$env:SCOPE_DATABASE_INTEGRATION = '1'
.\.venv\Scripts\python.exe -m unittest backend.test_supply_workflow backend.test_shortage
npm run lint
npx playwright test tests/assistant.spec.ts
npm run build
```

数据库测试使用临时 SQLite、独立工作空间及现有合成场景，不修改场景源表。浏览器测试模拟 API，验证交互契约；模型测试模拟响应，不代表外部模型服务已做真实调用验证。
