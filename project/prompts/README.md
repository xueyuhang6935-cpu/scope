# 行为与输出规则

`scope_parser.txt` 与 `investigator.txt` 保存当前模板；`loader.py` 按自身路径读取 UTF-8 文件，并生成内容版本。不依赖调用者工作目录。

新代码使用 `project.prompts.loader`，`backend.supply_prompts` 仅显式转发旧导入。修改模板会改变内容版本，应同时运行提示词检查与离线评测；固定案例的评价答案不得写入模板。权限、工具校验和预算由程序执行，提示词不能替代这些边界。

物流T10新增 `logistics_intent.txt`、`logistics_plan.txt`、`logistics_observation.txt`、`logistics_report.txt`，分别用于意图、计划/修订、观察评估和模型报告。复用 `load_prompt` 的路径与内容版本机制，不修改旧供应链模板或版本。生成结果返回完整SHA-256及版本，宿主同时写入run预算的提示词版本摘要。范围/参考时点只来自宿主，模型提议经过 `project.agents.logistics_protocol` 校验；模板和供应商结构化输出都不承担授权。
