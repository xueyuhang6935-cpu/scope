import { useState } from 'react';
import { Button, ScopedLink, StatusTag } from './components';
import { taskLabels, activeTask, type Plan, type Task, type RiskOption } from './supplyApi';
import { ChatInvestigation } from './InvestigationPanel';
import { CalculationSummary, showTime } from './RiskWorkspace';

export function ScopeFacts({ plan, defaultPeriod }: { plan: Plan; defaultPeriod?: string }) {
  return <dl><div><dt>工厂</dt><dd>{plan.factory === 'all' ? '全部工厂' : `${plan.factory} 工厂`}</dd></div><div><dt>物料</dt><dd>{plan.material || '全部物料'}</dd></div><div><dt>供应商</dt><dd>{plan.supplier || '未限定'}</dd></div><div><dt>时间范围</dt><dd>{plan.period || defaultPeriod || '当前导入记录'}</dd></div></dl>;
}

function ConfirmPlan({ task, busy, onConfirm }: { task: Task; busy: boolean; onConfirm: (plan: Plan) => void }) {
  const [plan, setPlan] = useState(task.plan);
  return <div className="sp-confirm-plan"><p>需要先澄清以下条件，尚未执行查询：</p>
    <ul>{(task.planning?.questions?.length ? task.planning.questions : ['请补充或核对本次查询范围。']).map(question => <li key={question}>{question}</li>)}</ul>
    <div className="sp-form-grid">
      <label>分析类型<select aria-label="确认分析类型" value={plan.intent} onChange={e => setPlan({ ...plan, intent: e.target.value })}><option value="">请选择</option><option value="inventory">库存查询</option><option value="quality">质量问题分析</option><option value="readiness">{task.source ? '缺料计算（模拟数据库）' : '缺料分析就绪检查'}</option></select></label>
      <label>工厂<select aria-label="确认工厂" value={plan.factory} onChange={e => setPlan({ ...plan, factory: e.target.value })}><option value="all">全部工厂</option><option value="A">A 工厂</option><option value="B">B 工厂</option></select></label>
      <label>物料编码<input aria-label="确认物料" value={plan.material} maxLength={120} onChange={e => setPlan({ ...plan, material: e.target.value })} placeholder="留空表示未限定" /></label>
      <label>供应商<input aria-label="确认供应商" value={plan.supplier} maxLength={120} onChange={e => setPlan({ ...plan, supplier: e.target.value })} placeholder="尚不支持此维度" /></label>
      <label>时间范围<select aria-label="确认时间范围" value={plan.period} onChange={e => setPlan({ ...plan, period: e.target.value })}><option value="">当前导入记录</option><option>未来三天</option><option>未来一周</option><option>未来一个月</option></select></label>
    </div><p>仅支持单个物料编码或全部物料；不支持供应商筛选和多物料表达式。库存与质量查询使用文件导入记录。</p>
    {plan.intent === 'readiness' && (task.source ? <p>缺料计算来源：模拟场景 {task.source.scenarioId}；已知证据截止 {showTime(task.source.asOf)}。计算从场景起点开始，未指定时使用 {task.source.days} 天，最长 14 天。</p> : <p>尚未接入模拟数据库，仅检查缺料计算的数据就绪情况。</p>)}
    <Button primary disabled={busy || !plan.intent} onClick={() => onConfirm(plan)}>按此范围查询</Button>
  </div>;
}

export default function AnalysisMessages({ tasks, busy, onConfirm, onCancel, onRetry, onFollowup, onSelectRisk }: { tasks: Task[]; busy: boolean; onConfirm: (task: Task, plan: Plan) => void; onCancel: (task: Task) => void; onRetry: (task: Task) => void; onFollowup?: (task: Task, text: string) => void; onSelectRisk?: (task: Task, risk: RiskOption) => void }) {
  const followupBusy = busy || tasks.some(activeTask);
  return <>{tasks.map(task => <div key={task.id} id={task.id}>
    <article className="sp-message sp-message-user"><strong>用户</strong><p>{task.text}</p></article>
    <article className="sp-message" aria-label="真实分析回答"><strong>供应链分析</strong><div role="status"><StatusTag tone={task.status === 'failed' ? 'error' : task.status === 'completed' ? 'success' : 'info'}>{task.result?.workflow ? '已关联调查 · 进度见下方' : taskLabels[task.status] || task.status}</StatusTag></div>
      {task.status === 'awaiting_confirmation' && (['investigate', 'simulate'].includes(task.planning?.action || '') ? <div><p>请核对调查对象：</p>{task.planning?.questions?.map(q => <p key={q}>{q}</p>)}{task.planning?.riskOptions?.map(r => <Button key={`${r.factory}:${r.material}:${r.revision}`} disabled={busy} onClick={() => onSelectRisk?.(task, r)}>{r.factory} 厂 · {r.material} · {r.revision} · 缺口 {r.shortage}</Button>)}</div> : <ConfirmPlan key={task.id} task={task} busy={busy} onConfirm={plan => onConfirm(task, plan)} />)}
      {['queued', 'running'].includes(task.status) && <><p>正在按以下范围读取授权数据…</p><ScopeFacts plan={task.plan} defaultPeriod={task.source && task.plan.intent === 'readiness' ? `${task.source.days} 天（场景起点）` : undefined} /></>}
      {task.status === 'planning' && <p>正在结合会话理解问题；查询会核对范围，追问会参考已有结果。</p>}
      {task.planning?.notice && <p>{task.planning.notice}{task.planning.tokens != null ? ` 本次解析 ${task.planning.tokens} Token。` : ''}</p>}
      {['planning', 'awaiting_confirmation', 'queued', 'running'].includes(task.status) && <Button disabled={busy} onClick={() => onCancel(task)}>停止任务</Button>}
      {task.error && <p role="alert">{task.error}</p>}
      {task.status === 'cancelled' && <p>任务已取消，不会交付后续结果。已开始的只读查询可能已经完成。</p>}
      {['failed', 'cancelled'].includes(task.status) && <Button disabled={busy} onClick={() => onRetry(task)}>重新发起分析</Button>}
      {task.result?.answer && <><p style={{ whiteSpace: 'pre-wrap' }}>{task.result.answer}</p>{task.result.evidenceCapturedAt && <p>依据结果时间：{showTime(task.result.evidenceCapturedAt)} · 未重新查询</p>}{task.result.evidenceSource && <p>场景：{task.result.evidenceSource.scenarioId} · 证据截止：{showTime(task.result.evidenceSource.asOf)}</p>}{task.result.basedOnTaskId && <a href={`#${task.result.basedOnTaskId}`}>查看依据的分析结果</a>}</>}
      {task.result?.refreshNotice && <p role="status">{task.result.refreshNotice}</p>}
      {task.result?.workflow && <ChatInvestigation taskId={task.id} runId={task.result.workflow.runId} selectedPlanId={task.result.workflow.planId} />}
      {task.result && !task.result.answer && !task.result.workflow && <div className="sp-actions">
        <Button disabled={followupBusy} onClick={() => onFollowup?.(task, '这个结果怎么算的')}>解释结果</Button>
        <Button disabled={followupBusy} onClick={() => onFollowup?.(task, '重新查询')}>重新查询计算</Button>
        {task.result.calculation && <><Button disabled={followupBusy} onClick={() => onFollowup?.(task, '继续查为什么晚交')}>继续调查原因</Button><Button disabled={followupBusy} onClick={() => onFollowup?.(task, '怎么解决，哪个方案更好')}>比较处置方案</Button></>}
      </div>}
      {task.result && !task.result.answer && <><h3>结论</h3><p>{task.result.summary}</p><details open><summary>当次执行范围</summary><ScopeFacts plan={task.result.scope} defaultPeriod={task.source && task.plan.intent === 'readiness' ? `${task.source.days} 天（场景起点）` : undefined} />{task.source && task.plan.intent === 'readiness' && <p>场景：{task.source.scenarioId} · 证据截止：{showTime(task.source.asOf)} · 窗口：{task.plan.period || `${task.source.days} 天`}</p>}</details>
        {task.result.calculation && <><CalculationSummary calculation={task.result.calculation} /><ScopedLink to="/risks">查看风险监测</ScopedLink></>}
        {!!task.result.missing.length && <><h3>缺失条件</h3><ul>{task.result.missing.map(item => <li key={item}>{item}</li>)}</ul></>}
        <h3>依据</h3><p>记录时间：{new Date(task.result.capturedAt).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })} · {task.result.ruleVersion}</p>
        {task.result.records.map((row, index) => <div className="sp-evidence" key={`${row.record_id}-${index}`}><strong>{row.factory} 工厂 · {row.material}</strong><p>{row.normalized_quantity !== undefined ? `${row.normalized_quantity} ${row.normalized_unit || '件'}` : row.reason}</p>{row.impact && <p>{row.impact}</p>}<ScopedLink to={`/data/imports/${row.batch_id}`}>{row.filename} · 来源批次</ScopedLink><small>记录 #{row.record_id}</small></div>)}
        {task.result.truncated && <p>仅展示前 200 条证据，请缩小物料或工厂范围。</p>}
        <h3>限制说明</h3><ul>{task.result.limitations.map(item => <li key={item}>{item}</li>)}</ul><h3>建议</h3><ul>{task.result.recommendations.map(item => <li key={item}>{item}</li>)}</ul><ScopedLink to="/data/connections">前往数据接入</ScopedLink>
      </>}
    </article>
  </div>)}</>;
}
