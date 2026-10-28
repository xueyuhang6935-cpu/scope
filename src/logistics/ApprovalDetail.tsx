import { useEffect, useRef, useState } from 'react';
import { useParams, useSearchParams } from 'react-router-dom';
import { request, type ChatReport } from './api';
import ReportView, { beijingTime, RunEvidence } from './ReportView';
import { phases, proposalStates, SavedFeedback, type CollaborationState } from './Collaboration';

export default function ApprovalDetail({ selectedRun, selectedVersion }: { selectedRun?: string; selectedVersion?: number }) {
  const { approvalId } = useParams(), [params] = useSearchParams();
  const runId = selectedRun || approvalId, requestedVersion = selectedVersion || Number(params.get('version')) || undefined;
  const [state, setState] = useState<CollaborationState | null>(null), [report, setReport] = useState<ChatReport | null>(null);
  const [error, setError] = useState(''), [reason, setReason] = useState(''), [checked, setChecked] = useState(false), [busy, setBusy] = useState(false), [revision, setRevision] = useState(0);
  const writing = useRef<AbortController | null>(null);
  useEffect(() => {
    if (!runId) return;
    const controller = new AbortController(); setState(null); setReport(null); setChecked(false); setReason(''); setError('');
    Promise.all([request<CollaborationState>(`/runs/${encodeURIComponent(runId)}/collaboration`, undefined, controller.signal), request<ChatReport>(`/runs/${encodeURIComponent(runId)}/report`, undefined, controller.signal)])
      .then(([value, saved]) => { if (!controller.signal.aborted) { setState(value); setReport(saved); } }).catch(cause => { if (!controller.signal.aborted) setError(cause.message); });
    return () => controller.abort();
  }, [runId, requestedVersion, revision]);
  useEffect(() => () => writing.current?.abort(), []);
  const proposal = requestedVersion ? state?.proposals.find(item => item.proposal_version === requestedVersion) : state?.proposals.at(-1);
  async function decide(decision: string) {
    if (!runId || !state || !proposal || busy) return;
    if (!checked || !reason.trim()) { setError('请核对提交版本与证据，并填写审核理由。'); return; }
    const controller = new AbortController(); writing.current = controller; setBusy(true); setError('');
    try {
      const value = await request<CollaborationState>(`/runs/${encodeURIComponent(runId)}/collaboration/decide`, { version: state.version,
        proposal_version: proposal.proposal_version, proposal_hash: proposal.proposal_hash, decision, reason }, controller.signal);
      if (!controller.signal.aborted) { setState(value); setChecked(false); setReason(''); }
    } catch (cause) { if (!controller.signal.aborted) { setError((cause as Error).message); setChecked(false); if ((cause as Error & { status?: number }).status === 409) setRevision(value => value+1); } }
    finally { if (!controller.signal.aborted) setBusy(false); }
  }
  if (!runId) return <section className="sp-section"><h2>选择一项建议</h2><p>在本页核对提交内容及证据，再作出审核决定。</p></section>;
  const current = proposal && proposal.proposal_version === state?.proposals.at(-1)?.proposal_version;
  return <section className="sp-section lg-approval-detail" aria-label="审批详情">
    {error && <div role="alert"><p>{error}</p>{!state && <button className="sp-button" onClick={() => setRevision(value => value+1)}>重新读取审批详情</button>}</div>}
    {!state ? !error && <p role="status">正在读取提交版本与证据…</p> : !proposal ? <p role="alert">该提交版本不存在。</p> : <>
      <div className="lg-section-heading"><h2>{proposal.content.title}</h2><span className="lg-status">提交版本 v{proposal.proposal_version}</span></div>
      <p>发起人 {proposal.requested_by} · 区域 {state.scope.region_ids.join('、')} · 参考时点 {state.scope.business_as_of}</p><p>{proposalStates[proposal.status]} · {phases[state.phase]}</p>
      <h3>建议内容与任务顺序</h3><p className="lg-plan-note">{proposal.content.note}</p>
      <SavedFeedback feedback={proposal.content.feedback} />
      <ol>{proposal.content.tasks.map(task => <li key={task.order_id}><strong>任务 {task.order_id}</strong><p>{(proposal.content.actions?.find(action => action.order_id === task.order_id)?.type || proposal.content.strategy) === 'contact_courier' ? '联系快递员核查' : '核对窗口与现场约束'}</p><RunEvidence runId={runId} evidenceId={task.evidence_id} /></li>)}</ol>
      {proposal.content.limitations.map(text => <p className="sp-muted" key={text}>{text}</p>)}
      <details><summary>核对原调查报告</summary><ReportView runId={runId} report={report} /></details>
      {proposal.previous_rejection && <section className="lg-warning"><h3>上次驳回意见</h3><p>{proposal.previous_rejection.reason}</p><p>当前显示本次提交内容，发起人的最新草稿不会替换这一版本。</p></section>}
      {state.proposals.filter(item => item.proposal_version === proposal.proposal_version-1).map(previous => <details key={previous.proposal_version}><summary>对比上一提交版本 v{previous.proposal_version}</summary><p>上次说明：{previous.content.note}</p><p>上次任务顺序：{previous.content.tasks.map(task => task.order_id).join(' → ')}</p><SavedFeedback feedback={previous.content.feedback} /></details>)}
      {proposal.decision && <section><h3>审核记录</h3><p>审核人 {proposal.decision.actor} · {beijingTime(proposal.decision.created_at)}（北京时间）</p><p>{proposal.decision.reason}</p></section>}
      {current && proposal.status === 'pending' && state.can_approve ? <div className="lg-approval-decision"><label>审核理由<textarea aria-label="审核理由" value={reason} maxLength={1000} disabled={busy} onChange={event => setReason(event.target.value)} /></label>
        <label><input type="checkbox" checked={checked} disabled={busy} onChange={event => setChecked(event.target.checked)} />已核对提交版本 v{proposal.proposal_version} 及相关证据</label>
        <div className="sp-actions"><button className="sp-button" disabled={busy} onClick={() => void decide('rejected')}>驳回当前建议</button><button className="sp-button sp-primary" disabled={busy} onClick={() => void decide('approved')}>批准当前建议</button></div>
      </div> : <p className="sp-muted">{proposal.status === 'pending' ? '当前账号无权审核此建议，发起人不能自批。' : '已决定或历史提交版本为只读。'}</p>}
      <p className="sp-muted">批准不等于执行；审核结果返回发起人的原会话。审核无需访问私人对话。</p>
    </>}
  </section>;
}
