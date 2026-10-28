import { useEffect, useRef, useState } from 'react';
import { request, type ChatDraft } from './api';

type Feedback = { feedback_id: string; kind: string; text: string; simulated: boolean; active?: boolean };
export function SavedFeedback({ feedback }: { feedback?: Feedback[] }) {
  return feedback?.length ? <details><summary>人工反馈与分析约束</summary>{feedback.map(item => <p key={item.feedback_id}>{item.kind === 'reported' ? '人工陈述，未经验证' : '分析约束'}{item.simulated && ' · 演示合成'}{item.active === false && ' · 已停用'}：{item.text}</p>)}</details> : null;
}

export type Proposal = { proposal_version: number; proposal_hash: string; status: string; requested_by: string; created_at?: string;
  content: { title: string; note: string; strategy?: string; business_as_of: string; tasks: { order_id: string; evidence_id: string; current_overdue_minutes?: number }[];
    actions?: { order_id: string; type: string; sequence: number }[]; feedback?: Feedback[]; limitations: string[] };
  decision?: { result: string; reason: string; actor: string; created_at?: string }; previous_rejection?: { reason: string } | null };
export type CollaborationState = { version: number; phase: string; can_edit: boolean; can_approve: boolean; proposals: Proposal[]; drafts?: ChatDraft[]; feedback?: Feedback[];
  scope: { batch_id: string; city: string; region_ids: string[]; business_as_of: string };
  followup: { followup_id: string; status: string; receipt: { note: string; simulated: boolean; reference: string } | null } | null;
  review: { business_as_of: string; old_risk_queue: { total: number; completed: number; still_pending: number; overdue: number; due_soon: number; window_unavailable: number;
    rows: { order_id: string; before: string; after: string }[]; truncated: boolean }; new_inflow: { total: number; metrics: Record<string, { name: string; value: number | null }> }; limitations: string[] } | null };
export const phases: Record<string, string> = { investigation: '调查结果已保存', changes_requested: '等待修改方案', draft: '草稿已保存，尚未提交',
  pending_approval: '等待另一账号审核', approved: '已批准，尚未创建跟进单', followup: '内部跟进中，待回执', receipt: '回执已记录，待复核', reviewed: '样本复核完成' };
export const proposalStates: Record<string, string> = { pending: '待审核', approved: '已批准', rejected: '已驳回', invalidated: '已失效' };

export default function Collaboration({ runId, onPrompt }: { runId: string; onPrompt: (text: string) => void }) {
  const [state, setState] = useState<CollaborationState | null>(null), [error, setError] = useState(''), [busy, setBusy] = useState(false), [revision, setRevision] = useState(0);
  const [receipt, setReceipt] = useState(''), [receiptResult, setReceiptResult] = useState('recorded'), [simulated, setSimulated] = useState(true), [reference, setReference] = useState(''), [at, setAt] = useState('');
  const writing = useRef<AbortController | null>(null);
  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    setState(null); setError(''); setAt('');
    async function load() {
      try { const value = await request<CollaborationState>('/runs/'+runId+'/collaboration', undefined, controller.signal); if (!controller.signal.aborted) { setState(value); setError(''); } }
      catch (cause) { if (!controller.signal.aborted) { setState(null); setError((cause as Error).message); } }
      finally { if (!controller.signal.aborted) timer = setTimeout(load, 3000); }
    }
    void load(); return () => { controller.abort(); clearTimeout(timer); };
  }, [runId, revision]);
  useEffect(() => () => writing.current?.abort(), []);
  useEffect(() => {
    if (state && !at) setAt(new Date(new Date(state.scope.business_as_of.replace(' ', 'T')+'Z').getTime()+30*60000).toISOString().slice(0, 19));
  }, [state, at]);
  async function act(action: string, values: Record<string, unknown> = {}) {
    if (!state || busy) return;
    const controller = new AbortController(); writing.current = controller; setBusy(true); setError('');
    try {
      let body = values;
      if (action === 'review') {
        const snapshot = await request<{ snapshot_id: string }>('/snapshots', { batch_id: state.scope.batch_id, city: state.scope.city, region_ids: state.scope.region_ids,
          business_as_of: at.replace('T', ' ')+(at.length === 16 ? ':00' : '') }, controller.signal, 120000);
        body = { snapshot_id: snapshot.snapshot_id };
      }
      const value = await request<CollaborationState>('/runs/'+runId+'/collaboration/'+action, { version: state.version, ...body }, controller.signal);
      if (!controller.signal.aborted) setState(value);
    } catch (cause) { if (!controller.signal.aborted) setError((cause as Error).message); }
    finally { if (!controller.signal.aborted) { setBusy(false); setRevision(value => value+1); } }
  }
  const proposal = state?.proposals.at(-1), draft = state?.drafts?.at(-1);
  return <section className="sp-section lg-collaboration" aria-label="调查处理"><h3>方案与后续处理</h3>
    {error && <p role="alert">{error}</p>}{!state ? <p>正在读取处理状态…</p> : <>
      <p role="status">{phases[state.phase] || state.phase}</p>
      <SavedFeedback feedback={state.feedback} />
      {proposal && <div className="lg-proposal-summary"><p>提交 v{proposal.proposal_version} · {proposalStates[proposal.status]}</p><strong>{proposal.content.title}</strong>{proposal.decision && <p>审核反馈：{proposal.decision.reason}</p>}</div>}
      {draft && <p>最新草稿 v{draft.draft_version} · {draft.status === 'submitted' ? '已提交' : '未提交'} · {draft.content.title}</p>}
      {state.can_edit && !state.followup && proposal?.status !== 'approved' && proposal?.status !== 'pending' && <button className="sp-button" onClick={() => onPrompt('请按当前约束和审核反馈修改方案。')}>继续修改方案</button>}
      {!proposal && !draft && state.can_edit && <p className="sp-muted">在聊天中提出核查方向，生成草稿后明确提交。</p>}
      <h4>内部跟进与回执</h4><p className="sp-muted">批准不等于执行；内部跟进不会向外部物流系统派单。</p>
      {state.can_edit && proposal?.status === 'approved' && !state.followup && <button className="sp-button sp-primary" disabled={busy} onClick={() => void act('followup')}>创建内部跟进单</button>}
      {state.followup && <><p>{state.followup.receipt ? '回执已记录' : '待回执'}</p>
        {state.followup.receipt && <p>{state.followup.receipt.simulated ? '模拟回执' : '人工回执，待外部核验'}：{state.followup.receipt.note}{state.followup.receipt.reference && ' · 来源 '+state.followup.receipt.reference}</p>}
        {state.can_edit && !state.followup.receipt && <form className="sp-form-grid" onSubmit={event => { event.preventDefault(); void act('receipt', { result: receiptResult, simulated, note: receipt, reference }); }}>
          <label>回执结果<select value={receiptResult} disabled={busy} onChange={event => setReceiptResult(event.target.value)}><option value="recorded">已记录核查结果</option><option value="not_executed">未执行</option></select></label>
          <label>回执说明<textarea value={receipt} maxLength={1000} disabled={busy} onChange={event => setReceipt(event.target.value)} required /></label>
          <label><input type="checkbox" checked={simulated} disabled={busy} onChange={event => setSimulated(event.target.checked)} />模拟回执，不代表真实执行</label>
          {!simulated && <label>外部来源编号<input value={reference} maxLength={200} disabled={busy} onChange={event => setReference(event.target.value)} required /></label>}
          <button className="sp-button" disabled={busy || !receipt.trim() || (!simulated && !reference.trim())}>保存跟进回执</button>
        </form>}</>}
      {state.can_edit && state.followup?.receipt && !state.review && <form className="sp-form-grid" onSubmit={event => { event.preventDefault(); void act('review'); }}>
        <h4>新快照复核</h4><label>复核参考时点<input type="datetime-local" step="1" value={at} disabled={busy} min={state.scope.business_as_of.replace(' ', 'T')} max="2000-12-31T23:59:59" onChange={event => setAt(event.target.value)} required /></label>
        <button className="sp-button sp-primary" disabled={busy}>{busy ? '正在处理…' : '创建新快照并复核'}</button>
      </form>}
      {state.review && <section aria-label="复核结果"><h4>复核结果</h4><p>参考时点 {state.review.business_as_of}</p>
        <p>旧风险队列 {state.review.old_risk_queue.total} 条：已完成 {state.review.old_risk_queue.completed}，仍待揽收 {state.review.old_risk_queue.still_pending}。</p>
        <p>两时点之间新流入 {state.review.new_inflow.total} 条。</p>{state.review.limitations.map(text => <p key={text}>{text}</p>)}
        <details><summary>风险任务变化</summary>{state.review.old_risk_queue.rows.map(task => <p key={task.order_id}>{task.order_id}：{task.before} → {task.after}</p>)}{state.review.old_risk_queue.truncated && <p>展示清单已截断，统计覆盖完整队列。</p>}</details>
        <p className="sp-muted">历史完成不能归因于内部建议或模拟回执。</p>
      </section>}
    </>}
  </section>;
}
