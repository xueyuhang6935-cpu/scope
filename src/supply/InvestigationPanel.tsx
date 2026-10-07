import { useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { Button, DataTable, PageHeading, ScopedLink, StatusTag } from './components';
import { supplyRequest } from './supplyApi';
import InvestigationReport, { type Report } from './InvestigationReport';
type Content = { kind: string; quantity: string; covered: string; remainingShortage: string; totalCost: string; currency: string; availableAt: string; validUntil: string; feasible: boolean; reasons: string[]; donor?: { factory: string; surplus: string; remainingAfterTransfer: string }; actions: Array<{ kind: string; factory: string; fromFactory?: string; material: string; quantity: string; unit: string; availableAt: string; cost: string }>; sourceEvidence: string[]; assumptions: string[] };
type Candidate = { id: string; version: number; content_hash: string; status: string; content: Content; factory?: string; material?: string; validationError?: string; canApprove?: boolean; requestedBy?: string; report?: Report; approval?: { approver: string; decision: string; comment: string; decided_at: string } };
type Run = { id: string; status: string; error?: string; report?: Report; plans: Candidate[]; input?: { replanning?: { round: number; parentRunId: string; reason: string } } };
type Access = { workspaceId: string; role: string; canPlan: boolean; members: Array<{ account: string; role: string; factories: string; approval_limit: string }> };
const labels: Record<string, string> = { queued: '排队中', running: '调查中', awaiting_approval: '等待审批', resuming: '恢复编排中', approved: '已批准', rejected: '已驳回', replan_required: '需要重新试算', manual_required: '需人工核查', failed: '调查失败', cancelled: '已撤回', candidate: '待审批', infeasible: '不可行', stale: '已失效', superseded: '已被替代', transfer: '跨厂调拨', expedite: '供应商加急', combined: '调拨＋加急', viewer: '只读', planner: '调查员', approver: '审批人', revoked: '已撤销' };
Object.assign(labels, { admin: '系统管理员', system_admin: '系统管理员', data_admin: '数据管理员', logistics: '采购／物流人员' });
Object.assign(labels, { replan_required: '等待自动重规划', replanned: '已自动发起下一轮' });
const time = (value: string) => new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false });
const amount = (value: string) => value.includes('.') ? value.replace(/0+$/, '').replace(/\.$/, '') : value;

function CandidateTable({ plans, onSelect }: { plans: Candidate[]; onSelect?: (id: string) => void }) {
  return <DataTable caption="处置方案比较" headers={['方案 / 版本', '覆盖 / 剩余缺口', '到线时间 / 费用', '状态 / 操作']}>
    {plans.map(p => <tr key={p.id}><td>{labels[p.content.kind]} · v{p.version}<small>{p.factory || p.content.actions[0]?.factory} 厂 · {p.material || p.content.actions[0]?.material} · {amount(p.content.quantity)} 件</small></td><td>{amount(p.content.covered)} / {amount(p.content.remainingShortage)}<small>{p.content.donor && `来源 ${p.content.donor.factory} 厂 · 调拨后两周余量 ${amount(p.content.donor.remainingAfterTransfer)}`}</small></td><td>{p.content.availableAt ? time(p.content.availableAt) : '—'}<small>{amount(p.content.totalCost)} {p.content.currency}</small><small>有效截止：{p.content.validUntil ? time(p.content.validUntil) : '—'}</small></td><td>{labels[p.status] || p.status}<small>{p.content.reasons.join('；')}</small>{onSelect ? <Button onClick={() => onSelect(p.id)}>核对并审批</Button> : <ScopedLink to={`/approvals/${p.id}`}>核对方案</ScopedLink>}</td></tr>)}
  </DataTable>;
}

function Members({ access, refresh }: { access: Access; refresh: () => void }) {
  const [account, setAccount] = useState(''), [role, setRole] = useState('approver'), [factories, setFactories] = useState('A,B'), [limit, setLimit] = useState('10000');
  const [message, setMessage] = useState(''), [busy, setBusy] = useState(false);
  async function save() {
    setBusy(true); setMessage('');
    try { await supplyRequest('/business-access', { workspaceId: access.workspaceId, account, role, factories: factories.split(','), approvalLimit: limit }); setMessage('权限已保存。'); refresh(); }
    catch (e) { setMessage((e as Error).message); } finally { setBusy(false); }
  }
  return <details><summary>业务成员与审批权限</summary><p>授权已注册账号。发起人与审批人必须不同；调拨审批需要同时拥有来源和目标工厂权限。</p>
    <div className="sp-form-grid"><label>业务账号<input aria-label="业务账号" value={account} onChange={e => setAccount(e.target.value)} /></label><label>业务角色<select aria-label="业务角色" value={role} onChange={e => setRole(e.target.value)}>{['viewer', 'planner', 'approver', 'revoked'].map(r => <option key={r} value={r}>{labels[r]}</option>)}</select></label><label>工厂权限<select aria-label="工厂权限" value={factories} onChange={e => setFactories(e.target.value)}><option value="A,B">A、B 工厂</option><option value="A">A 工厂</option><option value="B">B 工厂</option></select></label><label>审批额度（元）<input aria-label="审批额度" type="number" min="0" max="1000000" value={limit} onChange={e => setLimit(e.target.value)} /></label></div>
    <Button disabled={busy || !account.trim()} onClick={() => void save()}>保存业务权限</Button>{message && <p role="status">{message}</p>}
    {access.members.map(m => <p key={`${m.account}:${m.role}`}>{m.account} · {labels[m.role]} · 工厂 {JSON.parse(m.factories).join('、')} · 额度 {m.approval_limit} 元</p>)}
  </details>;
}

export default function InvestigationPanel({ eventId }: { eventId: string }) {
  const [runs, setRuns] = useState<Run[]>([]), [access, setAccess] = useState<Access | null>(null), [error, setError] = useState(''), [busy, setBusy] = useState(false), [refresh, setRefresh] = useState(0);
  const [requestId, setRequestId] = useState(crypto.randomUUID());
  useEffect(() => {
    const c = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    async function load() {
      try {
        const [list, grant] = await Promise.all([supplyRequest<Run[]>(`/risks/${eventId}/investigations`, undefined, c.signal), supplyRequest<Access>(`/business-access?eventId=${eventId}`, undefined, c.signal)]);
        if (!c.signal.aborted) { setRuns(list); setAccess(grant); setError(''); }
      } catch (e) { if (!c.signal.aborted) setError((e as Error).message); }
      finally { if (!c.signal.aborted) timer = setTimeout(load, 3000); }
    }
    void load(); return () => { c.abort(); clearTimeout(timer); };
  }, [eventId, refresh]);
  async function act(path: string, body: object) {
    setBusy(true); setError('');
    try { await supplyRequest(path, body); setRequestId(crypto.randomUUID()); setRefresh(v => v + 1); }
    catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  const latest = runs[0], active = latest && ['queued', 'running', 'awaiting_approval', 'resuming', 'replan_required'].includes(latest.status);
  const canPlan = access?.canPlan;
  return <section className="sp-section" aria-label="调查与方案试算"><div className="sp-section-heading"><h2>调查与方案试算</h2>{canPlan && <Button primary disabled={busy || !!active} onClick={() => void act(`/risks/${eventId}/investigate`, { requestId })}>调查并试算</Button>}</div>
    <p>按当前证据计算调拨、加急及组合方案；审批后保留决定，尚不下发业务单据。</p>{error && <p role="alert">{error}</p>}
    {access?.role === 'admin' && <><ScopedLink to="/settings?tab=permissions">管理完整角色与权限</ScopedLink><Members access={access} refresh={() => setRefresh(v => v + 1)} /></>}
    {!latest ? <p>尚未发起调查。</p> : <><p><StatusTag tone="info">{labels[latest.status]}</StatusTag> <span style={{ overflowWrap: 'anywhere' }}>{latest.id}</span></p>{latest.error && <p role="alert">{latest.error}</p>}
      {latest.input?.replanning && <p>自动重规划第 {latest.input.replanning.round} 轮 · 原因：{latest.input.replanning.reason}</p>}
      {latest.report && <InvestigationReport report={latest.report} />}{!!latest.plans.length && <CandidateTable plans={latest.plans} />}
      {canPlan && ['queued', 'running', 'awaiting_approval', 'replan_required', 'failed', 'manual_required'].includes(latest.status) && <Button disabled={busy} onClick={() => void act(`/investigations/${latest.id}/cancel`, {})}>撤回调查</Button>}
    </>}{runs.length > 1 && <details><summary>历史调查</summary>{runs.slice(1).map(r => <div key={r.id}><p>{r.id} · {labels[r.status]}</p><CandidateTable plans={r.plans} /></div>)}</details>}
  </section>;
}

export function ChatInvestigation({ runId, taskId, selectedPlanId }: { runId: string; taskId: string; selectedPlanId?: string }) {
  const [run, setRun] = useState<(Run & { stage: string; nextRunId?: string }) | null>(null);
  const [error, setError] = useState(''), [planId, setPlanId] = useState(selectedPlanId || '');
  const [currentId, setCurrentId] = useState(runId), [revision, setRevision] = useState(0);
  useEffect(() => { setCurrentId(runId); setRun(null); setPlanId(selectedPlanId || ''); }, [runId, selectedPlanId]);
  async function choosePlan(id: string) {
    try { await supplyRequest(`/tasks/${taskId}/select-plan`, { planId: id }); setPlanId(id); setError(''); }
    catch (e) { setError((e as Error).message); }
  }
  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const value = await supplyRequest<Run & { stage: string; nextRunId?: string }>(`/investigations/${currentId}`, undefined, controller.signal);
        if (controller.signal.aborted) return;
        setRun(value); setError('');
        if (value.nextRunId) { setCurrentId(value.nextRunId); setPlanId(''); return; }
        if (['queued', 'running', 'awaiting_approval', 'resuming', 'replan_required'].includes(value.status)) timer = setTimeout(poll, 2000);
      } catch (e) { if (!controller.signal.aborted) { setError((e as Error).message); setRun(null); } }
    }
    void poll(); return () => { controller.abort(); clearTimeout(timer); };
  }, [currentId, revision]);
  const stages: Record<string, string> = { queued: '已创建调查，等待执行', collect: '读取场景快照并试算候选方案', investigate: '调查取证', plans: '核验并保存报告和方案', approval: '等待人工审批', finish: '记录审批决定' };
  return <section aria-label="聊天调查进度">
    {error && <p role="alert">{error}<Button onClick={() => setRevision(v => v + 1)}>刷新调查</Button></p>}
    {!run && !error && <p role="status">正在读取真实调查进度…</p>}
    {run && <><p role="status">{labels[run.status] || run.status} · {stages[run.stage] || run.stage}</p><small>{run.id}</small>
      {run.input?.replanning && <p>自动重规划第 {run.input.replanning.round} 轮：{run.input.replanning.reason}</p>}
      {run.error && <p role="alert">{run.error}</p>}
      {run.report && <InvestigationReport report={run.report} />}
      {!!run.plans.length && <CandidateTable plans={run.plans} onSelect={id => void choosePlan(id)} />}
      <p>方案为试算结果，未预占库存、未下发业务单据。审批需核对具体版本及费用。</p>
      {planId && <BusinessApprovals key={planId} planId={planId} embedded />}
    </>}
  </section>;
}

export function BusinessApprovals({ planId, embedded = false }: { planId?: string; embedded?: boolean } = {}) {
  const params = useParams();
  const approvalId = planId || params.approvalId;
  const [plans, setPlans] = useState<Candidate[]>([]), [plan, setPlan] = useState<Candidate | null>(null), [error, setError] = useState(''), [comment, setComment] = useState(''), [busy, setBusy] = useState(false), [revision, setRevision] = useState(0);
  const [events, setEvents] = useState<Array<{ id: string; factory: string; material: string }>>([]);
  const [selectedEvent, setSelectedEvent] = useState('');
  useEffect(() => {
    const c = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    async function load() {
      try {
        if (approvalId) { const p = await supplyRequest<Candidate>(`/plans/${approvalId}`, undefined, c.signal); if (!c.signal.aborted) setPlan(p); }
        else { const [p, e] = await Promise.all([supplyRequest<Candidate[]>('/approvals', undefined, c.signal), supplyRequest<Array<{ id: string; factory: string; material: string }>>('/business-risks', undefined, c.signal)]); if (!c.signal.aborted) { setPlans(p); setEvents(e); } }
      } catch (e) { if (!c.signal.aborted) setError((e as Error).message); }
      finally { if (!c.signal.aborted) timer = setTimeout(load, 5000); }
    }
    setPlan(null); setError(''); void load(); return () => { c.abort(); clearTimeout(timer); };
  }, [approvalId, revision]);
  async function decide(decision: string) {
    if (!plan) return;
    setBusy(true); setError('');
    try { await supplyRequest(`/plans/${plan.id}/decision`, { decision, comment, version: plan.version, contentHash: plan.content_hash }); setRevision(v => v + 1); }
    catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  return <>{!embedded && <PageHeading title="审批中心" subtitle="按业务账号、工厂范围、金额与方案版本校验" />}{error && <p role="alert">{error}</p>}
    {!approvalId && !!events.length && <section className="sp-section"><h2>获授权的协作风险</h2><label>协作风险<select aria-label="协作风险" value={selectedEvent} onChange={e => setSelectedEvent(e.target.value)}><option value="">选择风险查看调查</option>{events.map(e => <option key={e.id} value={e.id}>{e.factory} 厂 · {e.material} · {e.id}</option>)}</select></label>{selectedEvent && <InvestigationPanel key={selectedEvent} eventId={selectedEvent} />}</section>}
    {!approvalId ? plans.length ? <CandidateTable plans={plans} /> : <p>当前账号没有可查看的方案。请先调查风险，或由工作空间所有者授予业务权限。</p> : !plan ? <p>正在读取方案…</p> : <>{!embedded && <ScopedLink to="/approvals">返回审批列表</ScopedLink>}<section className="sp-section"><h2>{labels[plan.content.kind]} · v{plan.version}</h2><p>发起人：{plan.requestedBy} · {labels[plan.status]}</p>{!embedded && <CandidateTable plans={[plan]} />}
      <p>有效截止（场景时间）：{time(plan.content.validUntil)}</p><p style={{ overflowWrap: 'anywhere' }}>方案摘要：{plan.content_hash}</p>
      <DataTable caption="方案动作" headers={['动作 / 工厂', '物料 / 数量', '到线 / 费用']}>{plan.content.actions.map((a, i) => <tr key={i}><td>{labels[a.kind]} · {a.fromFactory ? `${a.fromFactory} → ` : ''}{a.factory}</td><td>{a.material} · {amount(a.quantity)} {a.unit}</td><td>{time(a.availableAt)} · {amount(a.cost)} 元</td></tr>)}</DataTable>
      {plan.content.assumptions.map(a => <p key={a}>{a}</p>)}<details><summary>方案来源证据</summary><p style={{ overflowWrap: 'anywhere' }}>{plan.content.sourceEvidence.join('、')}</p></details>
      {plan.validationError && <p role="alert">{plan.validationError}</p>}{plan.report && <InvestigationReport report={plan.report} />}
      {plan.approval && <p>审批记录：{plan.approval.approver} · {labels[plan.approval.decision]} · {time(plan.approval.decided_at)} · {plan.approval.comment}</p>}
      {plan.canApprove && plan.status === 'candidate' ? <><div className="sp-form-grid"><label style={{ gridColumn: '1 / -1' }}>审批意见<textarea aria-label="审批意见" rows={3} placeholder="请填写核对后的审批意见" value={comment} maxLength={1000} onChange={e => setComment(e.target.value)} /></label></div><div className="sp-actions"><Button primary disabled={busy || !comment.trim() || !!plan.validationError} onClick={() => void decide('approved')}>批准方案</Button><Button disabled={busy || !comment.trim()} onClick={() => void decide('rejected')}>驳回方案</Button></div></> : <p>仅另一位具备工厂权限及足够额度的审批人可以审批有效方案。</p>}
    </section></>}
  </>;
}
