# 程序检查

根目录 `test_*.py` 检查提示词、上下文、报告协议、工具、Agent 判断、工作流恢复及评测入口；部分兼容检查使用后端组装。`integration/` 集中 Agent 的 HTTP 模型适配、SQL 连接、审计持久化、API/业务服务、完整判断循环与数据库工作流测试。

`integration/test_agent_loop.py` 使用模拟 HTTP，不需要真实模型；`test_supply_workflow.py` 保留 PostgreSQL 工作流生命周期；`test_investigation_sql.py` 同时包含离线校验与受开关保护的数据库测试。旧断言不变，原 backend 测试路径不保留发现包装，避免重复执行。通用认证、权限、导入和业务计算测试留在 `backend/`，浏览器测试留在根目录 `tests/`。

在 Scope 根目录执行两组发现命令，缺一不可：

```powershell
Remove-Item Env:SCOPE_DATABASE_INTEGRATION -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s backend -t . -p 'test_*.py' -v
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s project/tests -t . -p 'test_*.py' -v
```

默认运行模拟 HTTP、临时 SQLite 和真实 LangGraph 检查点测试，不调用真实模型。仅在隔离 PostgreSQL 场景、应用连接及 SQL 专用角色/视图就绪后设置 `SCOPE_DATABASE_INTEGRATION=1` 并重新运行两组命令；不要重建当前源库以满足测试。跳过项不算通过。

任务质量评测另见 [evals](../evals/README.md)，程序断言通过不等于真实模型质量达标。
