# T13 请求失败诊断

更新日期：2026-10-10（北京时间）。为后续模型请求失败保存结构化诊断，历史失败的底层原因无法凭本次修改补回。T13 仍为 `in_progress`，G3 不放行。

## 记录位置与调用流程

`backend/investigation_model.py:model_session` 接受可选的 `on_failure` 回调。HTTP 请求、状态检查、响应读取或外层 JSON 解析失败时，先调用回调，再重新抛出原始异常。原有调用方仍可捕获 `httpx.HTTPStatusError` 等异常；回调异常不会覆盖请求异常。

`backend/logistics_model.py` 将诊断放入当前尝试的 `usage_detail.transport_failure`。Agent 核心沿用原有 `record_usage(None)` 和 `model_transport_failed`，诊断与未知用量一起写入该运行的 `budget_json.model_usage_records`。调用次数在发送前计入，失败不返还、不自动重发。

T13 的 `scripts/verify-logistics-planning.py:capture_model_proposals` 透传回调，因此新运行原始 JSON 的 `model_usage_records[*].transport_failure` 可直接查阅。历史记录没有该字段属于正常兼容情况；当前运行详情接口未新增诊断展示。

## 字段说明

| 字段 | 含义 |
| --- | --- |
| `exception_type` | 原始异常类型名，不保存异常文本 |
| `failure_reason` | 分类后的失败原因 |
| `stage` | `sending`、`checking_status`、`reading_body` 或 `parsing_json` |
| `elapsed_ms` | 本次 HTTP 尝试耗时；不混用整次调查或前一次修复的耗时 |
| `timeout_seconds` | 本次实际使用的 HTTPX 操作超时参数，沿用最多 15 秒及调用剩余预算 |
| `http_status` | 已收到的响应状态码；未收到响应时为 `null` |
| `received_bytes` | 当前响应已读取的解码字节数；状态检查失败时未读取正文，通常为零 |
| `provider_request_id` | 仅从 `x-request-id` 或 `request-id` 提取，限制字符和长度，并拒绝包含已配置密钥的值 |
| `cause_types` | 最多三个底层异常类型名，不保存消息、请求或响应对象 |

失败原因包括 `connect_timeout`、`read_timeout`、`write_timeout`、`pool_timeout`、`connect_error`、`http_status_error`、`remote_protocol_error`、`invalid_response_json` 和 `unexpected_exception`。响应超过 100,000 字节记录为 `response_too_large`，读取期间超过调用时间预算记录为 `call_time_exceeded`；二者仍抛原有 `ValueError('response budget')`。

不会保存密钥、Authorization、URL、完整请求或响应正文、全部响应头及原始异常文本。诊断是服务端记录，仍受原运行读取与数据管理边界约束。

## 判断与限制

- `ReadTimeout` 是明确的读取超时证据；仅凭整次调查耗时接近 15 秒不能作此判断。
- HTTP 429 或 503 可确认对应错误状态，具体供应商原因仍需结合供应商记录；请求编号仅在服务提供且满足记录规则时存在。
- `ConnectError` 和异常类型链提供连接阶段线索，但不保证能区分所有 DNS、代理或 TLS 细节。
- 本次记录覆盖请求函数内部失败；HTTP 客户端初始化之前的错误、进程崩溃及诊断持久化失败不能保证留下完整记录。
- 保留原预算、协议修复、失败停止和验收标准，不代表请求故障已消除或 T13 已通过。

## 验证

专项 44 项通过，使用模拟 HTTP 和隔离 SQLite；覆盖错误分类、HTTP 429/503、外层 JSON 解析、部分读取、大小/时间限制、单次耗时与调用预算、请求编号边界、回调失败、诊断落盘和 T13 包装透传。

完整后端 168 项：155 通过、13 跳过；Agent 152 项：134 通过、18 跳过，均无失败。离线协议评测 24/24 通过，不代表真实模型语义通过；修改的六个 Python 文件语法和空白检查通过。运行汇总及完整日志保存在[本地验证目录](../../test-results/logistics-request-diagnostics-20261010-220208/summary.json)，详见[变更记录](../../plan/20261010-220700.md)。

没有调用真实供应商或重新运行 T13 稳定对照。运行中的正式后端需重新加载模块后，后续请求才会使用本次诊断行为。
