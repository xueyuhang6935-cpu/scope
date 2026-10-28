import { useEffect, useRef, useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { PageHeading } from '../supply/components';
import { request, type ChatConversation, type ChatReport, type KnowledgeEntry, type ModelReport } from './api';
import { useAccess } from './LogisticsShell';
import ReportView, { apiBase, RunEvidence, stopReasons } from './ReportView';
import { phases, proposalStates, SavedFeedback, type CollaborationState } from './Collaboration';

type Detail = KnowledgeEntry & { report: ChatReport | null; model_report: ModelReport | null; evidence_ids: string[]; stop_reason?: string;
  unresolved_questions: string[]; processing: Pick<CollaborationState, 'phase' | 'proposals' | 'followup' | 'review'> };
export default function KnowledgeDetail() {
  const { knowledgeId = '' } = useParams(), { access } = useAccess(); const [params] = useSearchParams(); const navigate = useNavigate();
  const [value, setValue] = useState<Detail | null>(null), [error, setError] = useState(''), [busy, setBusy] = useState(false), [revision, setRevision] = useState(0);
  const created = useRef<ChatConversation | null>(null), writing = useRef<AbortController | null>(null);
  useEffect(() => {
    const controller = new AbortController(); setValue(null); setError(''); created.current = null;
    request<Detail>(`/knowledge/${encodeURIComponent(knowledgeId)}`, undefined, controller.signal).then(result => { if (!controller.signal.aborted) setValue(result); }).catch(cause => { if (!controller.signal.aborted) setError(cause.message); });
    return () => controller.abort();
  }, [knowledgeId, revision]);
  useEffect(() => () => writing.current?.abort(), []);
  async function cite() {
    if (!value || busy) return;
    const controller = new AbortController(); writing.current = controller; setBusy(true); setError('');
    try {
      if (!created.current) created.current = await request<ChatConversation>('/conversations', {}, controller.signal);
      const conversation = await request<ChatConversation>(`/conversations/${created.current.conversation_id}/references`, { knowledge_id: value.knowledge_id, context_version: created.current.version }, controller.signal);
      if (!controller.signal.aborted) navigate(`/assistant/${conversation.conversation_id}`);
    } catch (cause) { if (!controller.signal.aborted) setError((cause as Error).message); }
    finally { if (!controller.signal.aborted) setBusy(false); }
  }
  const canCite = access?.roles.some(role => role.role === 'investigator');
  return <div className="lg-knowledge-detail"><PageHeading title={value?.title || '调查成果'} subtitle="原调查成果与证据 · 按当前访问权限读取" action={<Link className="sp-button" to="/knowledge">返回知识库</Link>} />
    {error && <div role="alert"><p>{error}</p>{!value && <button className="sp-button" onClick={() => setRevision(value => value+1)}>重试读取成果</button>}</div>}
    {!value ? !error && <p role="status">正在读取成果…</p> : <><section className="sp-section"><div className="lg-section-heading"><h2>成果与来源</h2><span className={`lg-status lg-${value.result_status === 'partial' ? 'orange' : 'green'}`}>{value.result_status === 'partial' ? '部分结果' : '完整报告'}</span></div>
      <p>上海 · 区域 {value.scope.region_ids.join('、')} · 参考时点 {value.scope.business_as_of}</p><p className="sp-muted">原快照 {value.scope.snapshot_id} · 数据批次 {value.scope.batch_id}</p>
      <div className="sp-actions"><a className="sp-button" href={`${apiBase}/api/logistics/knowledge/${encodeURIComponent(knowledgeId)}/download`}>下载成果</a>{canCite && <button className="sp-button sp-primary" disabled={busy} onClick={() => void cite()}>{busy ? '正在引用…' : '引用到新对话'}</button>}</div>
      {params.get('cite') && <p className="sp-muted">核对原参考时点后，点击“引用到新对话”。引用本身不会发起调查。</p>}
    </section>
    {value.result_status === 'partial' && <section className="sp-section lg-warning"><h2>调查尚未完整完成</h2><p>已有成果保留；模板指标报告不代表模型调查完整完成。</p>{value.stop_reason && <p>停止原因：{stopReasons[value.stop_reason] || '调查未完整完成'}</p>}{value.unresolved_questions.map(text => <p key={text}>{text}</p>)}</section>}
    <ReportView runId={value.run_id} report={value.report} modelReport={value.model_report} />
    {!!value.evidence_ids.length && <section className="sp-section"><h2>原调查证据</h2>{value.evidence_ids.map(ref => <RunEvidence key={ref} runId={value.run_id} evidenceId={ref} />)}</section>}
    <section className="sp-section"><h2>处理结果</h2><p>{phases[value.processing.phase]}</p>{value.processing.proposals.map(proposal => <div key={proposal.proposal_version}><p>提交 v{proposal.proposal_version} · {proposalStates[proposal.status]}{proposal.decision && ` · ${proposal.decision.reason}`}</p><SavedFeedback feedback={proposal.content.feedback} /></div>)}
      {value.processing.followup?.receipt && <p>{value.processing.followup.receipt.simulated ? '模拟回执' : '人工回执，待外部核验'}：{value.processing.followup.receipt.note}</p>}
      {value.processing.review && <><p>复核时点 {value.processing.review.business_as_of} · 旧风险完成 {value.processing.review.old_risk_queue.completed} 条</p><p>新流入 {value.processing.review.new_inflow.total} 条；历史完成不能归因于内部建议或模拟回执。</p></>}
    </section>
    <section className="sp-section"><h2>结果限制与未解决事项</h2>{value.limitations.map(text => <p key={text}>{text}</p>)}{value.unresolved_questions.map(text => <p key={text}>{text}</p>)}<p>2000 年仅为参考时间轴，真实年份未知。归档不会扩大原数据访问权限。</p></section></>}
  </div>;
}
