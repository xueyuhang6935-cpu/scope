# 角色与判断

`investigator.py` 负责调查判断循环：先规划、选择工具、修正、计划修订、预算收尾与提交报告。模型会话、可用性和工具能力由宿主注入；不读取配置或直接连接数据库。

`report_protocol.py` 定义结构化报告协议和证据引用校验。业务方案是否阻断仍由后端 `investigation_report.plan_blockers` 判断。引用校验通过不等于自然语言结论已经成立。

生产调用由 `backend.investigation_agent` 组装；新核心代码直接使用本目录。兼容与资源边界见 [入口清单](../../docs/agent-migration/compatibility.md)。
