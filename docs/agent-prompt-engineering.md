# 调查提示词与报告协议

范围解析和调查提示词分别位于 `project/prompts/scope_parser.txt`、`project/prompts/investigator.txt`。`project/prompts/loader.py` 将版本及内容摘要写入解析元数据、调查报告和工具审计；修改提示词后重启后端。

## 报告与兼容

模型报告的结构与引用校验实现位于 `project/agents/report_protocol.py`。`backend/investigation_report.py` 保留业务方案阻断和旧导入兼容转发；`backend/supply_workflow.py` 继续执行原有审批流程。当前任务摘要与上下文组装位于 `project/context/investigation.py`，后端在调用前负责来源快照与实时权限核查。迁移不改变报告协议、结论类型或旧报告展示方式。

M4 判断循环位于 `project/agents/investigator.py`，负责先规划、选择工具、修正输入、计划修订、预算收尾和报告提交。初始报告及模型消息由 context 组装，SQL/快照/历史证据分发由 `project/tools/investigation_dispatch.py` 执行。后端 `investigation_agent.py` 保留旧调用签名并组装能力，只把模式和三项预算选项传入核心；`investigation_model.py` 持有配置、凭证和 HTTP 会话，保留原超时、100000 字节响应上限、禁止重定向及无自动重试行为。测试 HTTP patch 路径改为 `backend.investigation_model.httpx.Client`。

新调用必须使用 `status`、`claims`、`unresolvedQuestions`、`nextSteps`。结论类型为 `observed`（直接观察）、`reported`（外部陈述）、`inferred`（推断）；每条必须引用实际读取的证据，后两类及截断结果须说明局限。`complete` 不允许未解问题；`partial` 必须说明未完成事项，无证据时可以没有结论。

兼容字段 `summary`、`evidenceIds` 从结论派生。旧持久化报告继续按原格式展示，不迁移数据库。引用有效性不等于语义证明；关键缺料数值、费用及可行性仍由确定性结果展示，不通过模型自由文本重新计算。

## 调查边界

必须先提交计划；执行 SQL 前读取涉及表的字段。表引用通过现有 SQL 语法树与 CTE 作用域解析，禁止用常量查询生成业务证据。最后一轮或接近时间、Token、上下文预算时仅允许提交报告；硬预算到达、网络失败或空转时后端保存保守部分结果，不自动重试模型请求。单次供应商用量或调用耗时可能跨越阈值，无法保证供应商实际计费严格不超过本地预算。

`get_candidate_plans` 只读取预计算方案。`get_evidence` 仅读取当前调查或显式父调查的证据，复核权限、任务归属和完整快照指纹；不同快照及缺少指纹的旧报告不作为当前证据。记录中的供应商陈述保持未独立验证的语义。

有效的部分报告不自动阻止候选方案；授权失效、调查运行失败、没有有效结论、缺料对象不可计算或没有可行候选方案时转人工。既有方案审批仍执行权限、版本及快照检查。授权失效时不交付失去权限的报告，独立工具审计仍受访问控制。

## 会话上下文

保留最多六个近期任务，记录先按聚焦物料、工厂、版本排序再截取。额外传入聚焦任务及快照、已有观察、未解问题和驳回原因；不把供应商反馈及推断升级为确认事实。报告和审批信息从持久化工作流读取，并在模型调用前重新核验权限。上下文仍是有限摘要，完整证据按需读取。

## 验证与评测

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s backend -t . -p 'test_*.py' -q
npm run lint
npx playwright test tests/investigation-report.spec.ts tests/assistant.spec.ts
.\.venv\Scripts\python.exe -X utf8 -m backend.eval_supply_agent
```

评测有24个固定案例，覆盖范围限制、来源陈述、提示注入、SQL修复、截断、驳回、预算、无证据及伪造引用。默认离线响应模拟，只证明流程和协议符合预期，不证明真实模型理解能力。结果默认写入 `test-results/agent-eval-offline.json`，记录提示词版本、用量、延迟和无效调用。

真实模型评测需要显式运行，使用 `.env` 配置，会产生供应商调用费用；业务数据使用隔离的内存数据库固定样本，不查询生产业务库：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m backend.eval_supply_agent --live --output test-results/agent-eval-live.json
```

可用 `--case agent-supplier` 单独运行一个案例。固定 SQL 样本只覆盖小型库存与反馈表；复杂 PostgreSQL 特性应由数据库集成测试验证。自动规则仅检查协议、动作和类型，不使用关键词匹配冒充语义验证。真实模型结果需逐项人工复核，将结论是否受证据支持、答案是否正确、条件是否完整保留分别记入 `claimsSupported`、`answerCorrect`、`conditionsPreserved`。这些字段未审核时保留未评定状态。

审核文件是以案例ID为键的 JSON，每项包含结果中的 `outputHash` 和上述三个布尔标签。使用 `--review-result test-results/agent-eval-live.json --reviews 审核文件路径 --output test-results/agent-eval-reviewed.json` 将审核应用于已保存结果，不再调用模型。仅接受输出摘要匹配的标签，避免将旧输出审核结果当作新输出的证明。费用以供应商账单为准，评测记录的Token只作成本参考。

数据库集成测试须显式设置 `SCOPE_DATABASE_INTEGRATION=1`，使用现有本地测试场景与临时工作空间。并行查询及任意方案试算未引入，待实际延迟及业务需要证明收益后再实现。
