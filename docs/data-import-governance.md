# 数据导入与治理

`/data/imports/new` 默认进入真实文件导入；也可切换到明确标记的模拟样例。真实数据使用 `DATA_DB_PATH` 指定的 SQLite 数据库，与认证库分开存放。所有 `/api/data/*` 接口要求登录。

## 导入流程

1. 上传不超过 10 MB、最多 10,000 条数据记录的 UTF-8 CSV 或 XLSX 文件，选择数据域、工厂、负责人及可选工作表。后端保存原文件和解析后的原始记录。
2. 映射物料编码、数量、单位及可选日期。必填列不能重复映射；日期支持 `YYYY-MM-DD`、`YYYY/MM/DD` 和 `YYYY.MM.DD`。
3. 执行质量校验。缺失字段、负数或非数字数量、重复物料编码、日期格式错误属于阻断问题。库存单位不是“件”且没有适用换算规则时属于警告，数量保持“尚不可计算”。
4. 阻断问题禁止提交；有警告时须确认隔离。按“数据域 + 工厂 + 物料编码”选择仅新增或新增/更新。提交使用幂等标识，重复请求返回同一个批次。
5. 批次详情保留原始值、标准化值、质量问题和原文件下载入口。已导入记录可在库存预览中查看。

## 治理与复检

- 质量检查和问题处理读取真实问题，可按工厂、状态筛选。批次详情可重新校验。
- 标准与映射可增加物料编码及单位换算规则版本，保留适用工厂、生效日期、依据和确认人。保存新规则后，可在批次详情重新校验以应用到已导入记录；原始编码仍保存在原始记录中。单位规则的生效日期不能晚于所处理记录的日期。
- 换算问题先分配负责人，再提交系数及依据。后端在同一事务中保存规则版本、标准化数量、问题状态和变更事件；失败不会显示修复成功。
- 来源与变更展示导入、分配、修复和复检历史。原始记录不被覆盖。
- `EVT-012` 仍是示例风险事件。证据页会显示 P012 的真实质量记录，但真实风险关闭仍需独立的风险判定服务。

主要接口：`POST /api/data/imports/preview`、`POST /api/data/imports/{draftId}/check`、`POST /api/data/imports/{draftId}/commit`、`GET /api/data/imports`、`GET /api/data/imports/{batchId}`、`GET /api/data/imports/{batchId}/file`、`POST /api/data/imports/{batchId}/recheck`、`GET /api/data/records`、`GET /api/data/governance/issues`、`POST /api/data/governance/issues/{issueId}/assign`、`POST /api/data/governance/issues/{issueId}/repair`、`GET/POST /api/data/governance/rules`、`GET/POST /api/data/governance/material-rules` 和 `GET /api/data/governance/changes`。
