# T13：引用校验修复后的完整对照重跑

完成日期：2026-10-10（北京时间）。使用最新引用校验修复后的代码完成 **6 次开发 + 18 次稳定运行**，共 24 次、12 组配对。完整重跑交付完成；T13 仍为 `in_progress`，G3 不放行。

## 版本与实验边界

- 本轮新目录为 `test-results/logistics-t13-reference-v1/`，此前预算轮和失败记录全部保留。没有修改运行时实现、提示词、固定基线、问题、模型或验收门槛，也没有在运行期间继续修复失败。
- 相对上一轮预算冻结，源码指纹只有 `project/agents/logistics_model.py`、`project/agents/logistics_protocol.py` 和 `project/prompts/logistics_report.txt` 不同，对应[此前引用修复](../../plan/20261010-155716.md)。这是完整调查重跑，不是报告阶段重放。
- 本轮 `cases.json`、`contract.json` 与 `logistics-t13-budget-v2` 的字节内容一致。固定与动态使用同一保存快照和问题；模型为 `deepseek-chat`，使用已有工具调用协议。
- 预算仍为 12 次模型调用、16 次工具调用、3 次修订、24000 Token、120 秒。动态每类至少 2/3 有效报告；数字与范围必须正确；至少两类有效路径修订可重复出现；增量为质量提升或等质量降低成本。
- 三个目标来自上海区域 5、参考日期 2000-07-02 的同一事件组，不作为三个独立业务事件。人员目标为 18:00，窗口和截断目标为 12:00。空间缺失类别本次未新增抽样，原记录仍为未找到。
- 每次运行使用 SQLite 原生备份建立隔离库，授权账号只在隔离库创建。没有重启正式服务、修改源记录或发布。

[开发冻结](../../test-results/logistics-t13-reference-v1/development/frozen.json)与[稳定冻结](../../test-results/logistics-t13-reference-v1/stability/frozen.json)的源码、提示词、案例、契约和模型一致；两阶段完成时均核验源批次摘要版本及代码指纹。最终[产物完整性核对](../../test-results/logistics-t13-reference-v1/integrity-check.json)再次检查 24 份输出、冻结记录、预算及用量回执。

## 有效报告结果

有效报告须同时满足模型实际交付、必要证据、数字正确、范围安全和逐份语义审核；程序停止报告或没有报告均不计成功。运行状态 `completed` 本身不等于目标正确完成。

| 阶段 / 目标 | 固定流程 | 动态调查 |
| --- | --- | --- |
| 开发，3 次/方法 | 1/3 | 2/3 |
| 稳定：人员 | 2/3 | 1/3 |
| 稳定：共同窗口 | 2/3 | 1/3 |
| 稳定：截断覆盖 | 2/3 | 1/3 |
| 稳定合计 | 6/9 | 3/9 |

最近预算轮动态为人员 0/3、窗口 1/3、截断 1/3，本轮为各 1/3；旧结果不改写。这些是同一事件组的小规模重复，不能把变化直接归因为引用修复或推广为业务收益。

[机器汇总](../../test-results/logistics-t13-reference-v1/summary.json)、[最终审核汇总](../../test-results/logistics-t13-reference-v1/final-reviewed-summary.json)保留全部运行和配对。审核绑定每份原始输出 SHA-256，共核对 61 条声明、总体限制、未解问题与下一步；1 条声明关联整报告矛盾，计失败。审核为 Codex 内部逐条核对，非独立人工盲审。

## 失败明细

| 稳定运行 | 结果与原因 |
| --- | --- |
| 人员动态 1 | 报告有效，12477 逾期 2/3 过半及样本负荷正确；负荷宿主补取单列 |
| 人员动态 2 | 模型交付 `partial`，数值正确；未解问题与下一步却要求补齐人员名单、负荷后再判断，和已确认的完整归属及已取得负荷矛盾，语义失败 |
| 人员动态 3 | 观察阶段 `model_transport_failed`，未完成必要负荷取证 |
| 窗口动态 1 | 有效报告；10440、15024 存在 09:00—11:00 共同窗口，不推断共同根因 |
| 窗口动态 2 | 规划阶段 `model_transport_failed`，未取得证据 |
| 窗口动态 3 | 必要证据取得，报告 `incomplete_report`，仅交付程序部分报告 |
| 截断动态 1 | 必要证据取得，报告 `incomplete_report`，不计成功 |
| 截断动态 2 | 必要证据取得，报告 `report_fact_required`，不计成功 |
| 截断动态 3 | 有效报告，逾期 3 条及三名人员各 1 条正确；宿主补页单列 |
| 固定失败 | 人员 3 为 `extra_field`；窗口 2 为 `invalid_json`；截断 3 为 `missing_field`，均无报告 |

开发失败另外保留：窗口固定 `incomplete_report`、截断固定 `unexpected_computed`、截断动态 `limitation_required`。本轮没有记录到 `overdue_assessment_claim` 拒收，但报告其他协议及自由文字仍不稳定。

[逐份审核](../../test-results/logistics-t13-reference-v1/reviews.json)、[失败与持久化节点错误](../../test-results/logistics-t13-reference-v1/failure-breakdown.json)区分无报告、程序停止、模型交付及整报告语义矛盾；没有把未知用量记作零费用。

## 路径修订与成本

- 开发截断动态 1 在首页截断后新增并实际执行第 2 页；稳定截断动态 2 也新增并执行第 2 页，其保存触发为 `evidence_not_supported`。
- 稳定窗口动态 3 在三人各 1 条反证及人员名单分页后，替换未执行步骤并实际查询逾期窗口明细；触发回放一致，但报告仍失败。
- 开发窗口动态 1、稳定窗口动态 1 只改变问题或完成条件，没有改变工具和参数，不计实际取证路径变更。
- 人员参数绑定、宿主负荷补取及稳定截断动态 3 的宿主补页不计模型自主重规划。当前稳定轮未证明至少两类路径变更都能重复出现；仅存在计划差异不能放行。

| 稳定成本 | 固定流程 | 动态调查 |
| --- | --- | --- |
| 已知 Token | 66782 | 105887 |
| 模型调用 | 15 | 32 |
| 工具调用 | 42 | 32 |
| 未知用量调用 | 0 | 2 |
| 方法执行时间合计 | 186.608 秒 | 252.155 秒 |

动态已知 Token 多 39105，约 **58.6%**，另有 2 次传输失败的未知用量。工具调用较少没有转化为总成本收益；本轮仍未证明答案质量或等质量成本的增量价值。开发成本另为固定 22136 Token、5 次模型/14 次工具；动态 47276 Token、13 次模型/13 次工具，均无未知用量。

## 验证与复跑

- 引用与协议定向回归：49 项通过。运行时源码未修改，没有重复完整后端、核心或前端回归；此前进程恢复测试超时仍为遗留问题，本轮不宣称修复。
- 标准库只读读取保存快照：[独立复算](../../test-results/logistics-t13-reference-v1/independent-verification.json)覆盖 3 个快照和 24 次运行；汇总、负荷及分母一致，快照范围、可见接单/完成和 GPS 时点检查通过。
- 已返回工具证据的范围和未来完成字段检查全部通过。这不等于原始外部 HTTP 全文审计。
- 两阶段完成记录、冻结哈希、12 组同输入配对、24 份输出与审核哈希、预算上限及模型用量回执核对通过。

以下命令用于新一轮，须使用新的输出目录；真实调用会产生费用。旧目录的原始结果不覆盖。

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts/verify-logistics-t13-round.py --live --stage development --output test-results/logistics-t13-reference-new
.\.venv\Scripts\python.exe -X utf8 scripts/verify-logistics-t13-round.py --live --stage stability --output test-results/logistics-t13-reference-new
.\.venv\Scripts\python.exe -X utf8 scripts/verify-logistics-t13-round.py --stage summary --output test-results/logistics-t13-reference-new
```

重跑结束后仍须独立复算和逐份审核，自动汇总不能替代语义验收。本轮完整对照重跑已完成，**T13 保持 `in_progress`，G3 不放行，T14 不开始**。
