import { useEffect, useRef, useState } from 'react';
import { type ChatReport, type ModelReport, request } from './api';
import { metricDefinitions } from './metricDefinitions';

export const apiBase = (import.meta.env.VITE_AUTH_API_BASE_URL || '').replace(/\/$/, '');
export const runStates: Record<string, string> = { queued: '排队中', running: '调查中', completed: '调查完成', waiting: '调查停止，保留部分结果', failed: '调查失败', cancelled: '已取消', unavailable: '状态暂不可读', new: '新对话' };
export const stopReasons: Record<string, string> = { model_call_limit: '模型调用次数达到上限', tool_call_limit: '工具调用次数达到上限', revision_limit: '计划修订次数达到上限', token_limit: '调查用量达到上限', active_time_limit: '调查时间达到上限', no_progress: '连续两次没有新的有效观察', blocked_dependencies: '后续步骤依赖未满足', interrupted_operation: '中断操作结果不确定', model_transport_failed: '模型请求失败，未自动重试', invalid_model_output: '模型输出未通过校验', required_evidence_unavailable: '报告必需证据不可用', evidence_limit: '调查证据数量达到上限' };
export function beijingTime(value?: string) {
  return value ? new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(value)) : '';
}

type EvidenceRecord = { order_id?: string; accept_time_reference?: string; time_window_end_reference?: string;
  current_wait_minutes?: number; current_overdue_minutes?: number; risk_state?: string;
  limitations?: string[];
  data?: { metrics?: Record<string, { name?: string; value: number | null }>;
    quality?: Record<string, { available: number; unavailable: number; not_applicable: number; denominator: number }> };
  rows?: { order_id?: string; courier_id?: string; aoi_id?: string; current_overdue_minutes?: number | null;
    metrics?: Record<string, { value: number | null }> }[] };
export function RunEvidence({ runId, evidenceId }: { runId: string; evidenceId: string }) {
  const [value, setValue] = useState<EvidenceRecord | null>(null), [error, setError] = useState('');
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);
  async function load() {
    controller.current?.abort(); const current = new AbortController(); controller.current = current;
    try {
      const result = await request<{ record: EvidenceRecord }>(`/runs/${encodeURIComponent(runId)}/evidence/${encodeURIComponent(evidenceId)}`, undefined, current.signal);
      if (!current.signal.aborted) { setValue(result.record); setError(''); }
    } catch (cause) { if (!current.signal.aborted) { setValue(null); setError((cause as Error).message); } }
  }
  const labels = { order_id: '任务编号', accept_time_reference: '接单参考时间', time_window_end_reference: '窗口截止', current_wait_minutes: '等待分钟', current_overdue_minutes: '逾期分钟', risk_state: '风险状态' };
  return <details onToggle={event => { if (event.currentTarget.open) void load(); }}><summary>查看任务证据</summary>
    {error && <p role="alert">{error}<button className="sp-button" onClick={() => void load()}>重试读取证据</button></p>}
    {value ? <><dl className="lg-business-details">{Object.entries(labels).filter(([key]) => key in value).map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{key === 'risk_state' ? ({ overdue: '逾期', due_soon: '临期', normal: '普通待揽收', window_unavailable: '窗口不可计算' }[String(value[key])] || '未知') : String(value[key] ?? '未知')}</dd></div>)}</dl>
      {Object.entries(value.data?.metrics || {}).map(([id, metric]) => <p key={id}>{metric.name || metricDefinitions.find(item => item.id === id)?.name || '指标'}：{metric.value ?? '不可计算'}</p>)}
      {Object.entries(value.data?.quality || {}).map(([id, quality]) => <p key={id}>{({ window: '窗口', stop_coordinates: '任务位置', accept_gps: '接单 GPS', pickup_gps: '完成 GPS' }[id] || '字段质量')}：可用 {quality.available}，不可用 {quality.unavailable}，不适用 {quality.not_applicable}，分母 {quality.denominator}</p>)}
      {value.rows?.slice(0, 20).map((row, index) => <div key={index}><p>{row.order_id && `任务 ${row.order_id} · `}快递员 {row.courier_id || '—'} · AOI {row.aoi_id || '—'}{row.current_overdue_minutes != null && ` · 逾期 ${row.current_overdue_minutes} 分钟`}</p>{Object.entries(row.metrics || {}).map(([id, metric]) => <p key={id}>{metricDefinitions.find(item => item.id === id)?.name || '指标'}：{metric.value ?? '不可计算'}</p>)}</div>)}
      {!!value.rows?.length && <p className="sp-muted">此处最多展示保存证据中的前 20 条记录。</p>}{value.limitations?.map(limit => <p className="sp-muted" key={limit}>{limit}</p>)}</> : !error && <p>正在读取证据…</p>}
  </details>;
}

export default function ReportView({ runId, report, modelReport, selected, onSelect }: { runId: string; report?: ChatReport | null; modelReport?: ModelReport | null;
  selected?: string[]; onSelect?: (id: string, checked: boolean) => void }) {
  return <>
    {report && <section className="sp-section lg-report-card" aria-label="调查报告卡片"><h3>快照指标报告</h3><p>区域 {report.scope.region_ids.join('、')} · 参考时点 {report.scope.business_as_of}</p>
      <div className="lg-chat-metrics">{Object.entries(report.summary.metrics).map(([key, metric]) => <div key={key}><strong>{metric.name}</strong><p>{metric.value ?? '不可计算'} · 程序计算</p></div>)}</div>
      <ol>{report.focus_tasks.map(task => <li key={task.order_id}>{onSelect ? <label><input type="checkbox" checked={selected?.includes(task.order_id) || false} onChange={event => onSelect(task.order_id, event.target.checked)} />任务 {task.order_id}</label> : <strong>任务 {task.order_id}</strong>}
        <p>等待 {task.current_wait_minutes ?? '未知'} 分钟 · 逾期 {task.current_overdue_minutes ?? '未知'} 分钟</p><RunEvidence runId={runId} evidenceId={task.evidence_id} /></li>)}</ol>
      <div className="sp-actions"><a href={`${apiBase}/api/logistics/runs/${encodeURIComponent(runId)}/report/markdown`}>下载报告</a><a href={`${apiBase}/api/logistics/runs/${encodeURIComponent(runId)}/report/view`} target="_blank" rel="noreferrer">完整阅读</a></div>
      {report.limitations.map(limit => <p className="sp-muted" key={limit}>{limit}</p>)}
    </section>}
    {modelReport && <section className="sp-section" aria-label="模型调查成果"><h3>{modelReport.value.status === 'partial' ? '部分调查结果' : '模型调查报告'}</h3>
      {modelReport.value.claims?.map((claim, index) => <article key={index}><p>{claim.statement}</p><small className="sp-muted">{claim.kind === 'computed' ? '程序计算' : claim.kind === 'observed' ? '观察' : '推断，仍需验证'}{claim.limitation && ` · ${claim.limitation}`}</small>{claim.evidence_ids.map(ref => <RunEvidence key={ref} runId={runId} evidenceId={ref} />)}</article>)}
      {modelReport.value.limitations?.map(text => <p className="sp-muted" key={text}>{text}</p>)}
      {!!modelReport.value.unresolved_questions?.length && <><h4>未解决事项</h4><ul>{modelReport.value.unresolved_questions.map(text => <li key={text}>{text}</li>)}</ul></>}
      {!!modelReport.value.next_steps?.length && <><h4>后续核查</h4><ul>{modelReport.value.next_steps.map(text => <li key={text}>{text}</li>)}</ul></>}
    </section>}
  </>;
}
