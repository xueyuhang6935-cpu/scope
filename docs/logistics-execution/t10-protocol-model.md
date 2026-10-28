# T10：计划、观察、报告与模型适配协议

2026-10-09后续行为更新：当前已验证的api.deepseek.com/deepseek-chat在未显式配置时默认tool_call，其他端点/模型仍默认json_text；显式配置优先，历史探测记录不改写。修复提示和报告预算预检已增强，详见[T13报告交付修复](t13-report-delivery.md)。

完成日期：2026-10-08（北京时间），分支 `feat/logistics-t10-protocol`。T09/G2基础已完成；本次不推进T11，不改变工作台的确定性调查流程。

## 交付与边界

- `project/agents/logistics_protocol.py`：`logistics-protocol-v1`、四类JSON Schema、严格JSON解析、结构与语义约束校验及依赖满足时的下一步骤选择。纯核心不导入后端、HTTP或数据库。
- `project/agents/logistics_model.py`：注入模型请求函数，按协议生成提议并有限修复；只返回经过校验的结构，不执行工具。
- `backend/logistics_model.py`：复用 `backend.investigation_model.model_session`，由真实run恢复身份/快照/区域/参考时点与口径，逐项读取已授权证据，复核当前权限并事务保存模型调用预算。
- 四份物流提示词复用已有加载器；返回内容版本及完整SHA-256，宿主将调用所用版本/哈希写入run的`budget_json.model_prompt_versions`。原供应链提示词与版本不变。
- 正式应用提供 `app.state.logistics_model` 宿主能力，没有新增公开模型调用API，不自动发送请求。计划、观察、模型报告的检查点保存及实际工具循环属于T11；T08模板报告接口与存储格式不变。

## 四类结构

所有固定结构禁止额外字段，字符串和数组有长度上限；整数不接受布尔值。工具参数采用T07白名单再校验。

| 协议 | 必需字段 | 约束 |
| --- | --- | --- |
| intent | `goal/questions/clarification/action` | action为investigate、clarify或unsupported；非investigate必须说明原因 |
| plan | `goal/version/hypotheses/steps/revision_reason` | 首版1，修订加1并给理由；最多16步骤、8假设 |
| observation | `plan_version/step_id/new_facts/hypothesis_assessments/missing/counterevidence/unresolved_questions/next_steps` | 绑定当前计划及宿主认定已完成的步骤；事实和反证引用实际已读证据 |
| report | `status/claims/evidence_ids/limitations/unresolved_questions/next_steps` | complete必须有结论且无未解问题；partial必须说明未完成事项 |

假设包含 `hypothesis_id/statement`；步骤包含 `step_id/question/tool/parameters/depends_on/completion_condition/status`。ID唯一，依赖存在且无自依赖/循环。新步骤必须pending；修订不能伪造状态、改写或删除已执行步骤。状态以宿主`step_states`为准，不按模型提供的completed直接执行。

计划只能选择T07六工具。参数拒绝region_ids、scope、T、snapshot_id、run_id、SQL及表达式占位符；H/W仅30/60。get_evidence仅可引用已读取、同run且当前授权的证据；尚未取得的证据应在后续观察后修订计划，而非猜测ID。

观察中的假设评估包含 `hypothesis_id/verdict/evidence_ids/reason`；verdict为supported、not_supported或unknown。支持/不支持必须引用证据，unknown可以无证据并说明原因。

报告每条claim包含 `statement/kind/evidence_ids/limitation/computed`。报告总证据列表必须等于所有结论引用的去重集合。

| 结论类型 | 核验规则 |
| --- | --- |
| computed | computed为`{evidence_id, metric, value}`，必须引用get_snapshot_summary的已保存工具证据，与积压/逾期/临期/近期接单/近期完成原值一致，null不替换为0 |
| observed | 引用工具结果或快照任务，不能把人工陈述当作直接观察；computed必须null |
| reported | 只能引用manual_statement证据并说明局限；当前没有人工事实入口，不能伪造这种证据 |
| inferred | 引用有效证据并说明推断局限；computed必须null |

截断结果和不可计算的computed必须说明局限。引用按run、空间、快照、指纹、时点和口径核对，实际内容哈希和当前权限复用T08证据接口。结构/引用有效只证明来源和协议；自然语言是否充分受证据支持、因果判断是否正确仍需后续评测和人工核查。

## 模型调用与有限修复

输出模式由宿主配置 `LOGISTICS_MODEL_OUTPUT_MODE` 决定，默认`json_text`，可选`tool_call/json_object/json_schema`。工具调用模式只允许单个 `submit_intent/submit_plan/submit_observation/submit_report` 协议提交，不将模型返回的工具名直接交给业务分发器。非工具模式不接受夹带工具调用。

所有模式统一严格JSON解析与后端校验：拒绝重复键、NaN/Infinity/溢出浮点数、Markdown代码块、多余尾部内容、非对象及超大输出。截断输出或拒绝回答不能当作有效协议。

默认最多1次协议修复，即一次生成最多2次请求；每次请求发出前事务递增run总model_calls，修复和失败不返还。记录已报告tokens、累计model_seconds及usage_unknown，复用run最大12次调用/120秒并限制累计已知tokens24000。次数/时间/token不足或上次用量未知时拒绝新调用；网络异常及供应商HTTP拒绝不自动重试、不自动切换模式。未知用量的合法输出可标记交付，但禁止后续自动继续调用。时间与token是本地收敛边界，不能声称为供应商计费硬上限。

修复只回传受控错误码和schema路径，不回传不可信模型原文、凭证或连接信息。每次预留、返回记账及最终交付重新校验授权；途中撤权仍记账但不交付受限结果。预算/提示词摘要保存在已有run预算JSON，不新增模型结果或业务数据库表。

## 宿主调用示例

```python
# user须由认证宿主取得；run_id绑定已保存快照，不能取模型scope。
proposal = app.state.logistics_model.generate(
    'plan', user, run_id, '检查当前积压集中在哪些快递员',
    evidence_ids=already_read_ids, plan=previous_plan, step_states=saved_step_states,
)
validated_plan = proposal['value']
# 此调用只生成校验后的提议。T11再接入持久化节点、执行工具和后续观察。
```

不需要模型时，可调用宿主 `validate(kind, user, run_id, value, evidence_ids=..., plan=..., step_states=...)` 核验结构。plan与step_states必须来自宿主保存状态，不应开放为未经核验的客户端字段。纯核心`next_step`仅用于已校验计划，不执行工具；失败/未完成依赖不能解锁后续步骤。

## 实际服务能力核查

命令：`.venv/Scripts/python.exe -X utf8 scripts/verify-logistics-model.py --live`。脚本默认不联网，必须显式提供--live；复用本机配置，仅发送合成intent协议，没有业务任务、源数据或正式账号信息，不修改配置。

本次deepseek-chat实测：

| 输出方式 | 结果 | 已报告token |
| --- | --- | ---: |
| tool_call | 支持，单个协议提交通过校验 | 526 |
| json_object | 支持，JSON协议通过校验 | 102 |
| json_schema | HTTP400拒绝，不自动重试 | 未知 |

能力探测建议tool_call；适配器仍采用兼容性默认json_text，实际启用模式由宿主明确配置。不得把本次HTTP400当作所有模型均不支持json_schema，也不能根据单次探测宣称复杂计划或调查质量通过。探测已报告token合计628，失败请求用量未知，费用以服务商账单为准。

## 验收结果

- 纯核心11项、宿主6项共17专项通过：工具/参数/依赖攻击、状态修订、四类报告来源、实际指标一致性、截断/缺失、跨run引用、JSON攻击、四种适配格式、次数/token/时间、有限修复、未知用量及撤权。
- 完整backend122项：109通过、13跳过；完整project104项：86通过、18跳过，均无失败。最后增加类型/预算测试并强化新步骤状态后，重新跑完整project及宿主+核心17专项通过。
- 离线Agent协议24/24；Python编译和差异空白检查通过。原T09恢复、后台确定性报告及真实SSE测试在backend完整回归中通过。
- 初次专项发现用量未知/预算耗尽时会提前打开HTTP客户端；已移至客户端打开前阻断，完整回归无失败。
- 证据：`test-results/logistics-t10/model-capabilities.json`、`schemas.json`、`prompt-versions.json`、`summary.json`、`backend.log/project.log/protocol-final.log/eval.log`。

未改前端，未运行前端类型/构建/浏览器；未运行真实源复算、真实业务模型调查、旧PostgreSQL供应链集成或容量测试。测试只写临时业务库，真实请求只做无业务数据能力探测。源任务、正式账号角色和本机.env未修改。已有后端需重启加载宿主协议服务，T11及后续保持未开始。
