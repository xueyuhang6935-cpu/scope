import { useEffect, useRef, useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { Plus, Sparkles } from 'lucide-react';
import { Composer } from '../supply/Assistant';
import { useAccess } from './LogisticsShell';
import { request, type ChatConversation, type ChatDraft, type ChatMessage, type RunView } from './api';
import ReportView, { beijingTime, RunEvidence, runStates, stopReasons } from './ReportView';
import WorkflowOverview from './WorkflowOverview';
import Collaboration from './Collaboration';

const states: Record<string, string> = { saved: '消息已保存', processing: '正在处理', waiting: '调查执行中', clarification: '待补充信息',
  replied: '已回复', partial: '部分结果', failed: '处理失败', restricted: '当前无权查看' };
const stages: Record<string, string> = { validate: '核对范围', summary: '计算指标', tasks: '核查重点任务', report: '整理报告', done: '调查结束' };

export default function Assistant() {
  const { threadId } = useParams(), { access } = useAccess();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const queryRun = params.get('run') || '';
  const [conversation, setConversation] = useState<ChatConversation | null>(null);
  const [history, setHistory] = useState<ChatConversation[]>([]), [page, setPage] = useState(1);
  const [historySearch, setHistorySearch] = useState(''), [currentOpen, setCurrentOpen] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [error, setError] = useState(''), [busy, setBusy] = useState(false), [revision, setRevision] = useState(0);
  const [conflict, setConflict] = useState(false);
  const [region, setRegion] = useState(''), [at, setAt] = useState(''), [mode, setMode] = useState('deterministic');
  const [scope, setScope] = useState(false), [historyOpen, setHistoryOpen] = useState(false), [dirty, setDirty] = useState(false);
  const [suggested, setSuggested] = useState<{ text: string }>();
  const [selected, setSelected] = useState<string[]>([]);
  const [progress, setProgress] = useState<Record<string, RunView>>({});
  const pending = useRef<{ id: string; conversationId: string; body: Record<string, unknown> } | null>(null);
  const pendingKey = `logistics-message:${access?.account}:${threadId || 'new'}`;
  const created = useRef<ChatConversation | null>(null), writing = useRef<AbortController | null>(null);
  const allowed = [...new Set(access?.roles.filter(g => g.role === 'investigator').flatMap(g => g.regions) || [])];
  const permitted = allowed.length > 0;

  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    setConversation(null); setMessages([]); setSelected([]); setProgress({}); setError(''); setDirty(false);
    async function load() {
      try {
        const list = await request<{ rows: ChatConversation[] }>(`/conversations?${new URLSearchParams({ page: String(page), search: historySearch })}`, undefined, controller.signal);
        if (controller.signal.aborted) return;
        setHistory(list.rows);
        if (threadId) {
          const [value, first] = await Promise.all([
            request<ChatConversation>(`/conversations/${threadId}`, undefined, controller.signal),
            request<{ rows: ChatMessage[]; after: number; has_more: boolean }>(`/conversations/${threadId}/messages`, undefined, controller.signal),
          ]);
          const rows = [...first.rows]; let cursor = first.after, more = first.has_more;
          while (more && !controller.signal.aborted) {
            const next = await request<typeof first>(`/conversations/${threadId}/messages?after=${cursor}`, undefined, controller.signal);
            rows.push(...next.rows); cursor = next.after; more = next.has_more;
          }
          if (controller.signal.aborted) return;
          setConversation(value); setMessages(rows);
          const focused = queryRun || value.context.run_id;
          if (queryRun && !rows.some(message => message.result?.run_id === queryRun)) throw new Error('该调查未关联到当前会话。');
          const runs = [...new Set([...rows.filter(m => m.status === 'waiting' && m.result?.run_id).map(m => m.result!.run_id!), ...(focused ? [focused] : [])])];
          const updates = await Promise.all(runs.map(async id => {
            try { return [id, await request<RunView>(`/runs/${id}`, undefined, controller.signal)] as const; }
            catch { return [id, { run_id: id, status: 'unavailable', current_node: '' }] as const; }
          }));
          if (!controller.signal.aborted) setProgress(Object.fromEntries(updates));
        }
        if (!controller.signal.aborted) setError('');
      } catch (cause) {
        if (!controller.signal.aborted) { setError((cause as Error).message); setMessages([]); setConversation(null); setProgress({}); }
      } finally { if (!controller.signal.aborted) timer = setTimeout(load, 2000); }
    }
    void load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [threadId, page, historySearch, queryRun, revision]);
  useEffect(() => {
    if (!dirty && conversation) {
      const context = queryRun ? progress[queryRun] || conversation.context : conversation.context;
      setRegion(context.region_ids?.length === 1 ? context.region_ids[0] : '');
      setAt(context.business_as_of?.replace(' ', 'T') || '');
      setMode(context.mode || 'deterministic');
    }
  }, [conversation, dirty, queryRun, progress]);
  useEffect(() => () => writing.current?.abort(), []);

  async function send(text: string, requestId: string, draft?: ChatDraft, focusRun?: string) {
    if (busy) throw new Error('消息正在提交，请稍后。');
    const controller = new AbortController(); writing.current = controller;
    setBusy(true); setError(''); setConflict(false);
    try {
      if (!pending.current) {
        try { pending.current = JSON.parse(sessionStorage.getItem(pendingKey) || 'null'); } catch { /* 存储不可用时仍可发送。 */ }
      }
      if (!pending.current || pending.current.id !== requestId) {
        let current = conversation || created.current;
        if (!current && !threadId) {
          current = await request<ChatConversation>('/conversations', {}, controller.signal);
          created.current = current;
        }
        if (!current || current.restricted) throw new Error('请先读取有权访问的会话。');
        const selection: Record<string, unknown> = { mode };
        if (focusRun || queryRun) selection.run_id = focusRun || queryRun;
        if (dirty && region) selection.region_ids = [region];
        if (dirty && at) selection.business_as_of = at.replace('T', ' ')+(at.length === 16 ? ':00' : '');
        if (selected.length) selection.task_ids = selected;
        pending.current = { id: requestId, conversationId: current.conversation_id,
          body: { text, request_id: requestId, context_version: current.version, selection,
            ...(draft ? { command: { action: 'submit', draft_version: draft.draft_version, draft_hash: draft.draft_hash } } : {}) } };
        try { sessionStorage.setItem(pendingKey, JSON.stringify(pending.current)); } catch { /* 重试仍使用内存中的同一请求。 */ }
      }
      const operation = pending.current;
      await request(`/conversations/${operation.conversationId}/messages`, operation.body, controller.signal);
      if (controller.signal.aborted) return;
      pending.current = null; created.current = null;
      try { sessionStorage.removeItem(pendingKey); } catch { /* 不影响已保存消息。 */ }
      setDirty(false); setSelected([]);
      if (!threadId || queryRun) navigate(`/assistant/${operation.conversationId}`);
      else setRevision(n => n+1);
    } catch (cause) {
      if (!controller.signal.aborted) { setError((cause as Error).message); setConflict((cause as Error & { status?: number }).status === 409); }
      throw cause;
    } finally { if (!controller.signal.aborted) setBusy(false); }
  }

  const focusId = queryRun || conversation?.context.run_id;
  const focusedReport = [...messages].reverse().find(m => m.result?.report && m.result.run_id === focusId)?.result?.report;
  const latestDraft = [...messages].reverse().find(m => m.result?.draft && m.result.run_id === focusId)?.result?.draft;
  const submitted = messages.some(m => m.result?.proposal && m.result.run_id === focusId &&
    m.seq > (messages.find(x => x.result?.draft === latestDraft)?.seq ?? 0));
  const currentRun = focusId ? progress[focusId] : undefined;
  async function runAction(action: string) {
    if (!focusId || busy) return;
    setBusy(true); setError('');
    try { await request(`/runs/${encodeURIComponent(focusId)}/${action}`, {}); setRevision(value => value+1); }
    catch (cause) { setError((cause as Error).message); }
    finally { setBusy(false); }
  }
  return <div className="sp-chat-layout lg-chat-layout lg-assistant-layout">
    <aside className={`sp-conversations ${historyOpen ? 'sp-panel-open' : ''}`} aria-label="历史对话">
      <button className="sp-button sp-new-conversation" disabled={busy || !permitted} onClick={() => { navigate('/assistant'); created.current = null; pending.current = null; setSuggested({ text: '' }); setRegion(''); setAt(''); setMode('deterministic'); }}><Plus size={18} />新建对话</button>
      <button className="sp-mobile-panel" aria-expanded={historyOpen} onClick={() => setHistoryOpen(!historyOpen)}>历史对话</button>
      <div className="sp-panel-body"><h2>历史对话</h2><label className="lg-history-search">搜索历史对话<input aria-label="搜索历史对话" value={historySearch} maxLength={200} onChange={event => { setHistorySearch(event.target.value); setPage(1); }} /></label>{history.map(item => <Link key={item.conversation_id} to={`/assistant/${item.conversation_id}`} onClick={() => { setHistoryOpen(false); setCurrentOpen(false); }} aria-current={item.conversation_id === threadId ? 'page' : undefined}><strong>{item.title}</strong><small>{item.restricted ? '范围已撤权' : runStates[item.status || 'new'] || '已有调查'} · {beijingTime(item.updated_at)}</small></Link>)}
        {!history.length && <p>暂无对话记录</p>}<div className="sp-actions"><button className="sp-button" disabled={page === 1} onClick={() => setPage(p => p-1)}>上一页</button><button className="sp-button" disabled={history.length < 20} onClick={() => setPage(p => p+1)}>下一页</button></div>
      </div>
    </aside>
    <section className="sp-chat" aria-label="物流智能助手">
      <div className="sp-chat-topline"><span>{threadId ? conversation?.title || '物流对话' : '新会话'}</span><div className="sp-actions"><button className="sp-button lg-mobile-current" aria-expanded={currentOpen} onClick={() => setCurrentOpen(!currentOpen)}>当前调查与待办</button><button className="sp-button" aria-expanded={scope} onClick={() => setScope(!scope)}>分析范围</button></div></div>
      {error && <p role="alert">{error}</p>}
      {conflict && pending.current && <button className="sp-button" disabled={busy || !conversation} onClick={() => {
        const text = String(pending.current?.body.text || ''); pending.current = null;
        try { sessionStorage.removeItem(pendingKey); } catch { /* 内存请求已清除。 */ }
        void send(text, crypto.randomUUID()).then(() => setSuggested({ text: '' })).catch(() => {});
      }}>核对当前对象后重新发送</button>}
      {!permitted && <p>需要调查员权限才能发送物流调查消息。</p>}
      {conversation?.restricted && <p role="alert">当前范围已撤权，关联内容已隐藏。</p>}
      <div className="sp-chat-messages" aria-label="对话消息" aria-live="polite">
        {!messages.length && !error && <div className="sp-chat-welcome"><Sparkles size={28} /><h2>今天想调查什么？</h2><p>可以描述调查问题，补充区域与完整参考日期时点。调查完成后可继续追问和讨论方案。</p><button className="sp-button" onClick={() => setSuggested({ text: '调查区域5在2000年7月2日10:00的揽收风险' })}>调查揽收风险</button></div>}
        {messages.map(message => <article className={`lg-chat-message lg-chat-${message.role}`} key={message.message_id} aria-label={message.role === 'user' ? '用户消息' : '助手回复'}>
          <strong>{message.role === 'user' ? '你' : '助手'}</strong><small>{states[message.status] || message.status}{message.result?.source === 'model_explanation' ? ' · 模型解释' : ''}</small><p>{message.text}</p>
          {message.result?.run_id && <div className="sp-actions"><Link to={`/assistant/${threadId}?run=${encodeURIComponent(message.result.run_id)}`} onClick={() => setCurrentOpen(true)}>查看当前调查</Link>{progress[message.result.run_id] && <span role="status">{runStates[progress[message.result.run_id].status] || '正在处理'}{progress[message.result.run_id].status === 'running' && ` · ${stages[progress[message.result.run_id].current_node] || '核查证据'}`}</span>}</div>}
          {message.result?.report && <ReportView runId={message.result.run_id!} report={message.result.report} selected={selected} onSelect={message.result.report === focusedReport ? (id, checked) => setSelected(checked ? [...selected, id] : selected.filter(value => value !== id)) : undefined} />}
          {message.result?.partial_report && <section className="sp-section"><h3>部分调查结果</h3><p>调查已停止，已有证据、停止说明与未解决事项可在当前调查查看。</p>{message.result.partial_report.value?.limitations?.map(value => <p key={value}>{value}</p>)}{message.result.partial_report.value?.unresolved_questions?.map(value => <p key={value}>{value}</p>)}</section>}
          {message.result?.knowledge_reference && <section className="sp-section lg-reference-card" aria-label="历史成果引用"><h3>已引用历史成果</h3><Link to={`/knowledge/${message.result.knowledge_reference.knowledge_id}`}>{message.result.knowledge_reference.title}</Link><p>区域 {message.result.knowledge_reference.scope.region_ids.join('、')} · 原参考时点 {message.result.knowledge_reference.scope.business_as_of}</p><p>{message.result.knowledge_reference.result_status === 'partial' ? '部分结果' : '完整报告'} · 引用不会重新执行调查</p></section>}
          {message.result?.draft && <section className="sp-section" aria-label="方案草稿卡片"><h3>草稿 v{message.result.draft.draft_version} · {message.result.draft.content.title}</h3><p>{message.result.draft.content.note}</p>
            <ol>{message.result.draft.content.actions.map(action => <li key={action.order_id}>任务 {action.order_id}：{action.type === 'verify_window' ? '核查窗口' : '联系快递员核查'}</li>)}</ol>
            {message.result.draft === latestDraft && !submitted && <button className="sp-button sp-primary" disabled={busy} onClick={() => void send('提交当前方案', crypto.randomUUID(), message.result!.draft).catch(() => {})}>提交当前草稿审批</button>}
            {message.result.draft.content.limitations.map(value => <p key={value}>{value}</p>)}</section>}
          {message.result?.proposal && <Link to="/approvals">查看审批中心</Link>}
        </article>)}
      </div>
      <Composer logistics contextKey={threadId || 'new'} suggested={suggested} available={permitted && (!threadId || !!conversation) && !conversation?.restricted} busy={busy} onSend={send}
        toolbar={<div className="sp-composer-tools"><span className="sp-scope-chip">{region ? `区域 ${region}` : '区域待确认'}</span><span className="sp-scope-chip">{at || '完整时点待确认'}</span><span>{mode === 'model' ? '实验模型调查' : '确定性调查'}</span>{selected.length > 0 && <span>已选 {selected.length} 条任务</span>}</div>} />
      {scope && <aside className="sp-analysis-scope sp-scope-popover" aria-label="当前分析范围"><h2>当前分析范围</h2>
        <label>区域<select aria-label="分析区域" value={region} onChange={e => { setRegion(e.target.value); setDirty(true); }}><option value="">待确认</option>{allowed.map(r => <option key={r} value={r}>{r}</option>)}</select></label>
        <label>参考时点<input aria-label="参考时点" type="datetime-local" step="1" min="2000-01-01T00:00:00" max="2000-12-31T23:59:59" value={at} onChange={e => { setAt(e.target.value); setDirty(true); }} /></label>
        <label>调查方式<select aria-label="调查方式" value={mode} onChange={e => { setMode(e.target.value); setDirty(true); }}><option value="deterministic">确定性调查</option><option value="model">实验模型调查</option></select></label>
        <p>2000 为参考年。选择范围用于下一条消息，调查由后端校验后启动。</p><button className="sp-button" onClick={() => setScope(false)}>关闭范围</button>
      </aside>}
    </section>
    <aside className={`lg-current-investigation ${currentOpen ? 'is-open' : ''}`} aria-label="当前调查与待办"><button className="sp-button lg-mobile-current" onClick={() => setCurrentOpen(false)}>关闭当前调查与待办</button>
      <section className="sp-section"><h2>当前调查</h2>{!focusId ? <p className="sp-muted">提出问题并确认范围后，这里展示会话关联的调查。</p> : !currentRun ? <p>正在读取当前调查…</p> : <><p>{currentRun.question || conversation?.title}</p><p>{runStates[currentRun.status]}</p><p>区域 {currentRun.region_ids?.join('、')} · 参考时点 {currentRun.business_as_of}</p>
        {currentRun.status === 'unavailable' ? <p role="alert">当前调查状态暂不可读，请重新加载或检查权限。</p> : <><p>依据原调查快照读取，打开页面不会重新执行。</p>{currentRun.result?.stop_reason && <p>停止原因：{stopReasons[currentRun.result.stop_reason] || '调查因执行限制停止'}。已有结果保留，仍需人工核查。</p>}{currentRun.result?.unfinished_steps?.map(step => <p key={step.step_id}>未完成：{step.question}</p>)}
        {currentRun.error_code && <p role="alert">调查处理失败，已保存结果保留，请核对当前状态后继续。</p>}
        {currentRun.can_cancel && ['queued', 'running', 'waiting'].includes(currentRun.status) && <button className="sp-button" disabled={busy} onClick={() => void runAction('cancel')}>取消当前调查</button>}
        {currentRun.can_report && !focusedReport && ['completed', 'waiting'].includes(currentRun.status) && <button className="sp-button" disabled={busy} onClick={() => void runAction('report')}>生成基础指标报告</button>}
        {!!currentRun.result?.evidence_ids?.length && <details><summary>已保存的调查证据</summary>{currentRun.result.evidence_ids.map(ref => <RunEvidence key={ref} runId={focusId} evidenceId={ref} />)}</details>}
        {currentRun.result?.report && <ReportView runId={focusId} modelReport={currentRun.result.report} />}
        {focusedReport && <Link to={`/knowledge/LKNOW-${focusId}`}>查看归档成果</Link>}</>}
      </>}</section>
      {focusId && currentRun && ['completed', 'waiting'].includes(currentRun.status) && <Collaboration key={focusId} runId={focusId} onPrompt={text => setSuggested({ text })} />}
      <WorkflowOverview />
    </aside>
  </div>;
}
