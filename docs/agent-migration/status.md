# Agent 迁移状态

分支：`refactor/agent-layout`。起始提交：`6669e8d`。用户授权仅 M0—M2。

| 阶段 | 状态 | 结果 |
| --- | --- | --- |
| M0 | done | 后端 104 项：73 通过、31 跳过；离线评测 24/24 协议通过；函数清单、调用方、副作用及回退点已记录 |
| M1 | done | 两个模板字节与版本完全一致；2 项专项通过；24/24 离线协议通过；内部调用切换，旧模块仅转发 |
| M2 | not_started | 纯上下文与报告协议 |
| M3—M7 | not_started | 不在本次范围 |

已有未提交 `AGENTS.md` 与 `plan/20261007-152452.md` 保留，不计入迁移提交。物流 T02 未开始。

M0 证据：`test-results/agent-migration/baseline.json`、`m0-backend.log`、`m0-eval.json`、`m0-eval.log`。测试未调用真实模型；数据库集成未启用。T01 页面历史失败继续单列，本批无前端修改，不宣称其已修复。

M0 提交：`5ba630b`。M1 证据为 `m1-tests.log`、`m1-eval.json`、`m1-eval.log`；测试包含从临时工作目录启动新 Python 进程加载提示词。`workflows/` 在执行前已拼写正确，无目录重命名操作；其余待迁目录只放职责说明。
