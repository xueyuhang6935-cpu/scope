# Agent 目录迁移实施计划

M0—M5b 已完成，现按用户授权完成 M6 测试及评测整理；M7 与物流 T02—T24 不启动。起始版本为 `6669e8d`，迁移分支为 `refactor/agent-layout`；M6 开发回退点为 `cb8f13b`，本阶段独立提交。

## 阶段与验收

| 阶段 | 交付 | 验收 |
| --- | --- | --- |
| M0 | 基线、函数映射、任务状态、独立提交 | 当前后端与离线评测有实际结果；调用方与副作用已核对 |
| M1 | Python 包及职责说明、提示词与加载器迁移 | 模板字节和版本一致；不依赖当前目录；调用方接通；离线评测无新增失败 |
| M2 | 纯上下文组装与报告协议抽取 | 输入输出与旧版本一致；权限仍在后端；报告错误与旧格式兼容行为不变；回归和启动检查 |
| M3 | SQL 工具协议、校验、结果处理及调用保护迁入 tools；连接与审计留后端 | 授权撤销、结构读取门禁、只读连接、结果截断、错误协议、审计恢复及完整回归 |
| M4 | 判断循环入 agents、初始上下文入 context、工具分发入 tools；模型请求留后端 | 预算、先规划、修正、部分报告、证据引用、撤权、无重试不变；固定案例请求/输出对照、完整回归及隔离启动 |
| M5a | 调查 API 与业务服务拆分 | 11 个端点 OpenAPI 不变；原服务方法等价；鉴权、审批事务与提交后调度验证 |
| M5b | 工作流及恢复控制迁入 workflows，服务与检查点资源留后端 | 节点/状态/边、thread_id、同步检查点不变；暂停恢复、旧检查点、异常审计、重规划、关闭及启动验证 |
| M6 | Agent 测试分类迁移，评测案例与评分归入 evals | 原用例不丢失、不重复，断言与冻结案例不变；评分对照一致，完整离线回归、CLI 与文档更新 |
| M7 | 收尾 | 后续单独执行，本次不启动 |

## 保持不变的边界

不改接口、数据库结构、LangGraph 节点、状态字段、检查点路径、thread_id、审批与预算规则；不升级依赖、不调用真实模型、不改源数据。不将旧供应链规则直接转换为物流规则。

`project/` 运行时核心及评测核心不导入后端或 FastAPI；集成测试可导入宿主验证组装。当前任务纯数据组装迁入 context，业务问题解析留在后端。报告格式和引用校验迁入 agents，方案阻断仍在后端。临时兼容模块只能显式转发，不复制实现；内部调用使用新路径。

## 命令与证据

在 Scope 根目录执行。M0 保存完整后端测试和 24 个离线案例；M1 运行提示词迁移专项及离线评测；M2 运行完整后端、project 专项、离线评测及隔离启动。新增 project 测试的发现命令会在创建后写入 README。

```powershell
Remove-Item Env:SCOPE_DATABASE_INTEGRATION -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s backend -t . -p 'test_*.py' -v
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s project/tests -t . -p 'test_*.py' -v
.\.venv\Scripts\python.exe -X utf8 -m backend.eval_supply_agent --output test-results/agent-migration/m0-eval.json
```

证据目录：`test-results/agent-migration/`（忽略、不提交）。持久文档记录实际结果；T01 的历史报告不改写。T01 浏览器为 24 通过、9 失败，数据库专项因缺少旧场景前提跳过。本次无前端修改，不重复运行历史失败页面；不宣称数据库恢复或真实模型质量通过。

## 提交与回退

M0、M1、M2、M3、M4、M5a、M5b 各自提交。M5a 为 `ae9c277`，是 M5b 的开发回退点。开始时已有 `AGENTS.md` 未提交修改及 `plan/20261007-152452.md` 未跟踪文件；保留其原始字节，不混入迁移提交。哈希记录在 `baseline.json`；本批开始时再次记录于 `m5-before.json`。

M6 前再次记录用户已有文件与冻结案例哈希于 `m6-before.json`，用例清单于 `m6-before-tests.json`。M6 将 Agent 相关测试迁入 `project/tests/integration/`，循环模拟从数据库工作流测试拆出；通用后端与浏览器测试不动。评测核心与案例迁入 `project/evals/`，CLI 保留后端宿主注入。测试旧路径不增加发现包装，避免重复执行。

回退顺序为先 M6、再 M5b、M5a、M4、M3、M2、M1，使用对应提交的 `git revert`，不硬重置。M0 文档可保留。由于本批不修改数据库与检查点结构，不需要数据库降级操作。现有未提交文件不参与回退。
