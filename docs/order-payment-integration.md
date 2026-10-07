# 订单与支付分析接入（第一阶段）

实施依据：工作区根目录的《订单与支付数据分析Agent_项目设计与开发实施方案_V3.md》。本阶段按用户选择，先把现有 P0 能力接入 Scope，再分阶段扩展。

## 已接入

- 登录后通过左侧“订单与支付分析”进入新页面。
- 四种现有模拟快照：基础微型、重复订单、非法支付状态、孤立支付。
- 选择1—31个上海时区自然日与1—3项指标；结束日期包含当天，服务端转换为左闭右开区间。
- 原有 Python 注册表、只读 SQL、导入质量与覆盖检查；订单量不依赖支付数据。
- 展示指标定义、数据可用性、阻塞原因、快照和注册表哈希；导出 Markdown 与 JSON。
- 查询超出快照覆盖时明确阻塞，不改日期、不填零、不调用模型。

正常快照在2026-09-29的手算结果：有效订单6单、30分钟成功4单、支付率66.67%。非法支付状态案例应显示订单6单、支付指标不可用；仅勾选订单数时应查询完成。

## 运行

需要 Node.js 24+、Python 3.11+。保留两个相邻目录：

```text
数据异常调查/
  Scope/                  React 界面、Express 登录和分析接口
  order-payment-analyst/   Python 计算与模拟快照
```

当前工作区已安装依赖并生成数据。迁移或首次安装时，在 `order-payment-analyst` 中执行：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -X utf8 micro_demo.py
.\.venv\Scripts\python.exe -X utf8 quality_demo.py
```

生成器只创建不存在的快照，不覆盖已存在文件。网页请求不会创建、导入或修改业务数据库。

在 `Scope` 中执行：

```powershell
npm install
npm run dev
```

访问 `http://localhost:3003`，使用已有账号登录，点击左侧“订单与支付分析”。开发环境由 Vite 将 `/api` 转发到本地3002端口。若已有旧服务占用3002，请先停止旧服务后再启动本项目。

也可执行 `npm run build` 后 `npm start`，直接访问 `http://localhost:3002`。已有认证环境配置继续使用；本阶段分析不需要模型 API 密钥。注册验证码仍需原项目短信或邮件服务，未为演示绕过真实登录。

默认寻找相邻 Python 项目的 `.venv`。如果目录不同，在 Scope 的 `.env` 中配置：

```dotenv
ANALYST_ROOT="../order-payment-analyst"
# 可选：Python 可执行文件的绝对路径
# ANALYST_PYTHON="E:/数据异常调查/order-payment-analyst/.venv/Scripts/python.exe"
```

`ANALYST_ROOT` 相对 Scope 目录解析，不由浏览器提交。独立部署前后端时沿用 `VITE_AUTH_API_BASE_URL` 与 `FRONTEND_ORIGIN`；它们现同时用于认证与分析接口。

## 接口与边界

- `GET /api/analyst/catalog`：返回允许的模拟案例、指标和能力边界。
- `POST /api/analyst/query`：要求现有会话 Cookie；未登录返回401。

```json
{
  "snapshot_id": "micro_02",
  "start_date": "2026-09-29",
  "end_date": "2026-09-29",
  "metric_ids": ["valid_order_count_v1", "paid_order_30m_count_v1", "order_paid_30m_rate_v1"]
}
```

Python 适配器仅接受登记快照ID、日期与指标ID，拒绝额外字段、路径、SQL和未知指标。Express 使用固定脚本、JSON标准输入、15秒超时、1MB输出限制、最多4个并发子进程。请求记录不会写入现有认证数据库；网页刷新后不保留查询结果，可手动导出。

快照文件名用于区分演示案例，`data_snapshot_id` 是原P0水位表内的标识；各质量案例沿用相同水位标识，导出同时携带案例ID和实际文件SHA-256，不能仅靠水位标识混用数据。当前哈希用于标识本次文件及检测查询期间变化，尚不是V3的不可变快照登记、持久化证据或恢复机制。

## 验证

在 Scope 中：

```powershell
npm run lint
npm run build
npm run test:analyst
```

在 Python 项目中：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
```

适配器测试使用临时数据库，不修改已有模拟数据。覆盖手算值、按依赖部分阻塞、纯订单查询、重复数据阻塞、超覆盖日期、不合法参数、缺失快照不自动创建、只读哈希与接口登录保护。

## 后续 V3 阶段

1. 补齐 P0 微型案例，增加14天模拟数据和 P1 八工具、趋势/分组/比较。
2. 接入结构化计划、澄清与连续追问，保留确定性计算和范围校验。
3. 实现单Agent动态调查、预算、证据事务、检查点和恢复。
4. 在 Scope 中增加证据驱动图表与报告；React 页面替代方案中的 Streamlit。
5. 执行 B1/B3 对照评测、失败分析和演示验收。

当前页面是已有计算能力的 Web 入口，不代表完整 V3 或自主 Agent 已完成。
