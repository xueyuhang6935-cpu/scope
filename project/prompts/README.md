# 行为与输出规则

`scope_parser.txt` 与 `investigator.txt` 保存当前模板；`loader.py` 按自身路径读取 UTF-8 文件，并生成内容版本。不依赖调用者工作目录。

新代码使用 `project.prompts.loader`，`backend.supply_prompts` 仅显式转发旧导入。修改模板会改变内容版本，应同时运行提示词检查与离线评测；固定案例的评价答案不得写入模板。权限、工具校验和预算由程序执行，提示词不能替代这些边界。
