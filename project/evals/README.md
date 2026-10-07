# 任务质量评估

`supply_agent_cases.json` 保存 24 个冻结案例（14 个范围解析、10 个 Agent）；`supply_agent.py` 保存样本、模拟响应、评分和人工审核逻辑。冻结答案仅供评测，不进入运行时提示词、上下文或工作流。

评测核心通过参数接收调查、范围解析和模型会话能力，不导入 backend/FastAPI，不读取 `.env`。`backend/eval_supply_agent.py` 保留为宿主组装与兼容 CLI，仅在显式 `--live` 时加载本机模型配置。

在 Scope 根目录执行：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m backend.eval_supply_agent
.\.venv\Scripts\python.exe -X utf8 -m backend.eval_supply_agent --case agent-supplier --output test-results/agent-eval-single.json
```

默认输出仍为 `test-results/agent-eval-offline.json`，单例筛选、退出码和评分规则不变。案例按模块路径加载，不依赖当前工作目录；输出路径相对调用目录。不要直接以 `python -m project.evals.supply_agent` 运行，需要宿主注入能力。

离线模式使用脚本响应和隔离内存 SQLite，只验证协议，不证明真实模型质量。真实模型评测需显式 `--live`，可能产生费用；本次 M6 未执行。人工审核通过 `--review-result 已保存的真实模型结果 --reviews 审核文件 --output 输出文件` 应用，不重调模型。审核必须匹配 `outputHash`，三个布尔标签为 `claimsSupported`、`answerCorrect`、`conditionsPreserved`；摘要不匹配保持待审核，不能将离线结果当作真实质量结论。详见 [评测说明](../../docs/agent-prompt-engineering.md)。
