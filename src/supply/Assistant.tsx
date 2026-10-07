import { useEffect, useRef, useState, type ReactNode } from 'react';
import { useNavigate, useOutletContext, useParams } from 'react-router-dom';
import { ArrowRight, ArrowUp, ArrowUpRight, ChevronDown, Database, Diamond, ListFilter, MessageSquare, MoreHorizontal, Plus, Send, ShieldCheck, Sparkles } from 'lucide-react';
import { Button, EmptyState, ScopedLink, StatusTag } from './components';
import AnalysisMessages, { ScopeFacts } from './AnalysisMessages';
import { activeTask, supplyRequest, type Capabilities, type Plan, type Task, type Thread, type RiskOption } from './supplyApi';
import type { SupplyContext } from './SupplyShell';

const prompts = ['检查未来三天缺料风险', '查询 P001 库存', '检查当前工厂数据质量'];
const promptCards = [{ title: '检查缺料风险', description: '检查未来三天的分析条件', detail: '还缺哪些关键数据？', Icon: Diamond }, { title: '查询物料库存', description: '查看 P001 当前库存', detail: '数量与来源是否清楚？', Icon: Database }, { title: '核对数据质量', description: '检查当前工厂的数据问题', detail: '哪些记录需要处理？', Icon: ShieldCheck }];
const states = { ready: '就绪', running: '分析中', complete: '完成', failed: '失败', empty: '无数据' };

export function Composer({ contextKey, suggested, reset = 0, available = false, busy = false, onSend, toolbar }: { contextKey: string; suggested?: { text: string }; reset?: number; available?: boolean; busy?: boolean; onSend?: (text: string, requestId: string) => Promise<void>; toolbar?: ReactNode }) {
  const { currentUser, factory } = useOutletContext<SupplyContext>();
  const key = `supply-draft:${currentUser}:${factory}:${contextKey}`;
  const [draft, setDraft] = useState('');
  const [saved, setSaved] = useState(true);
  const input = useRef<HTMLTextAreaElement>(null);
  const sending = useRef(false);
  const [sendError, setSendError] = useState('');
  async function send() {
    if (!onSend || sending.current || !draft.trim()) return;
    sending.current = true; setSendError('');
    try {
      const pendingKey = `${key}:pending`;
      let pending: { text: string; id: string } | null = null;
      try { pending = JSON.parse(sessionStorage.getItem(pendingKey) || 'null'); } catch {}
      if (!pending || pending.text !== draft.trim()) pending = { text: draft.trim(), id: crypto.randomUUID() };
      sessionStorage.setItem(pendingKey, JSON.stringify(pending));
      await onSend(pending.text, pending.id);
      edit(''); sessionStorage.removeItem(pendingKey);
    } catch (error) { setSendError((error as Error).message || '发送失败，草稿已保留。'); }
    finally { sending.current = false; }
  }
  useEffect(() => { try { setDraft(sessionStorage.getItem(key) || ''); setSaved(true); } catch { setDraft(''); setSaved(false); } }, [key, reset]);
  function edit(value: string) { setDraft(value); try { sessionStorage.setItem(key, value); setSaved(true); } catch { setSaved(false); } }
  useEffect(() => { if (suggested) { edit(suggested.text); input.current?.focus(); } }, [suggested]);
  useEffect(() => { if (reset) input.current?.focus(); }, [reset]);
  return <div className={`sp-composer ${toolbar ? 'sp-composer-modern' : ''}`} data-unsaved={draft.trim() ? 'true' : 'false'}>
    <StatusTag tone={available ? 'success' : 'warning'}>{available ? '查询与调查 · 方案需人工审批' : '供应链助手尚未接入'}</StatusTag>
    {!saved && <p className="sp-draft-help" role="alert" id={`draft-help-${contextKey}`}>保存失败，请复制草稿后再离开。</p>}
    <textarea disabled={busy} maxLength={2000} ref={input} aria-label="供应链消息草稿" aria-describedby={!saved ? `draft-help-${contextKey}` : undefined} value={draft} onChange={event => edit(event.target.value)} placeholder="输入你的问题，例如：检查 A 工厂未来三天的缺料风险…" rows={3} />
    <div className="sp-composer-actions">{toolbar || <ScopedLink to="/data/connections">前往数据接入</ScopedLink>}<Button primary disabled={!available || busy || !draft.trim()} onClick={send} title={available ? '提交只读查询' : '等待供应链助手服务接入'}>{toolbar ? <>发送<ArrowUp size={16} /></> : <><Send size={15} />发送</>}</Button></div>{sendError && <p role="alert">{sendError}</p>}
  </div>;
}

export default function Assistant() {
  const { threadId } = useParams();
  const { factory, currentUser } = useOutletContext<SupplyContext>();
  const navigate = useNavigate();
  const isExample = threadId === 'THREAD-001';
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [threads, setThreads] = useState<Thread[]>([]);
  const [thread, setThread] = useState<Thread | null>(null);
  const [busy, setBusy] = useState(false);
  const [serviceError, setServiceError] = useState('');
  const [loadVersion, setLoadVersion] = useState(0);
  const routeRef = useRef(threadId); routeRef.current = threadId;
  useEffect(() => { routeRef.current = threadId; return () => { routeRef.current = '__unmounted__'; }; }, [threadId]);
  const [reset, setReset] = useState(0);
  const [suggested, setSuggested] = useState<{ text: string; context: string }>();
  const [supplier, setSupplier] = useState('');
  const [period, setPeriod] = useState('');
  const [preview, setPreview] = useState<keyof typeof states>('complete');
  const [historyOpen, setHistoryOpen] = useState(false);
  const [scopeOpen, setScopeOpen] = useState(false);
  const scopePanel = useRef<HTMLElement>(null);
  const scopeButton = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!scopeOpen) return;
    function outside(event: PointerEvent) { if (!scopePanel.current?.contains(event.target as Node) && !scopeButton.current?.contains(event.target as Node)) setScopeOpen(false); }
    function escape(event: KeyboardEvent) { if (event.key === 'Escape') { setScopeOpen(false); scopeButton.current?.focus(); } }
    document.addEventListener('pointerdown', outside); document.addEventListener('keydown', escape);
    return () => { document.removeEventListener('pointerdown', outside); document.removeEventListener('keydown', escape); };
  }, [scopeOpen]);
  const [notice, setNotice] = useState('');
  const [newMessage, setNewMessage] = useState(false);
  const messages = useRef<HTMLDivElement>(null);
  const nearBottom = useRef(true);
  const previousPreview = useRef(preview);
  const scopeQuery = factory === 'all' ? '' : `?factory=${factory}`;
  const scopeKey = `supply-scope:${currentUser}:${factory}:${threadId || 'new'}`;
  const tasks = thread && thread.id === threadId ? thread.tasks || [] : [];
  const latest = tasks.at(-1);
  const pending = tasks.some(activeTask);
  useEffect(() => {
    const controller = new AbortController();
    setThread(null); setServiceError('');
    async function load() {
      try {
        const [cap, history, detail] = await Promise.all([supplyRequest<Capabilities>('/capabilities', undefined, controller.signal), supplyRequest<Thread[]>(`/threads?factory=${factory}`, undefined, controller.signal), threadId && !isExample ? supplyRequest<Thread>(`/threads/${threadId}`, undefined, controller.signal) : Promise.resolve(null)]);
        if (detail && factory !== 'all' && detail.factory !== factory) throw new Error('会话不在当前工厂范围，请从历史列表重新打开。');
        if (!controller.signal.aborted) { setCapabilities(cap); setThreads(history); setThread(detail); }
      } catch (error) { if (!controller.signal.aborted) { setCapabilities(null); setServiceError((error as Error).message); } }
    }
    void load(); return () => controller.abort();
  }, [threadId, factory, loadVersion]);
  useEffect(() => {
    if (!latest || !['planning', 'queued', 'running'].includes(latest.status)) return;
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const task = await supplyRequest<Task>(`/tasks/${latest!.id}`, undefined, controller.signal);
        if (!controller.signal.aborted) { setThread(old => old ? { ...old, tasks: old.tasks?.map(item => item.id === task.id ? task : item) } : old); setServiceError(''); if (['planning', 'queued', 'running'].includes(task.status)) timer = setTimeout(poll, 1000); }
      } catch (error) { if (!controller.signal.aborted) { setServiceError('无法刷新任务，请重新载入；后台任务可能仍在执行。'); timer = setTimeout(poll, 3000); } }
    }
    timer = setTimeout(poll, 500); return () => { controller.abort(); clearTimeout(timer); };
  }, [latest?.id, latest?.status]);
  async function sendMessage(text: string, requestId: string, basedOnTaskId?: string) {
    setBusy(true); const origin = threadId;
    try {
      let id = threadId;
      if (!id) id = (await supplyRequest<Thread>('/threads', { factory, requestId })).id;
      const task = await supplyRequest<Task>(`/threads/${id}/messages`, { text, requestId, basedOnTaskId, scope: { factory: thread?.factory || factory, supplier, period, material: '', intent: '' } });
      if (routeRef.current !== origin) return;
      if (!threadId) navigate(`/assistant/${id}${scopeQuery}`);
      else setThread(old => old ? { ...old, tasks: [...(old.tasks || []).filter(item => item.id !== task.id), task] } : old);
    } finally { setBusy(false); }
  }
  async function selectRisk(task: Task, risk: RiskOption) {
    setBusy(true); setServiceError('');
    try {
      const updated = await supplyRequest<Task>(`/tasks/${task.id}/select-risk`, risk);
      if (routeRef.current === task.thread_id) setThread(old => old ? { ...old, tasks: old.tasks?.map(item => item.id === updated.id ? updated : item) } : old);
    } catch (error) { setServiceError((error as Error).message); } finally { setBusy(false); }
  }
  async function followupTask(task: Task, text: string) {
    try { await sendMessage(text, crypto.randomUUID(), task.id); }
    catch (error) { setServiceError((error as Error).message); }
  }
  async function taskAction(task: Task, action: 'confirm' | 'cancel', plan?: Plan) {
    setBusy(true); setServiceError('');
    try { const updated = await supplyRequest<Task>(`/tasks/${task.id}/${action}`, action === 'confirm' ? { plan, confirmed: true } : {}); if (routeRef.current === task.thread_id) setThread(old => old ? { ...old, tasks: old.tasks?.map(item => item.id === updated.id ? updated : item) } : old); }
    catch (error) { setServiceError((error as Error).message); } finally { setBusy(false); }
  }
  useEffect(() => { setSupplier(''); setPeriod(''); setSuggested(undefined); setPreview('complete'); setHistoryOpen(false); }, [threadId]);
  useEffect(() => {
    if (isExample) return;
    try { const stored = JSON.parse(sessionStorage.getItem(scopeKey) || '{}'); setSupplier(typeof stored.supplier === 'string' ? stored.supplier : ''); setPeriod(typeof stored.period === 'string' ? stored.period : ''); }
    catch { setNotice('无法恢复分析范围，请重新设置。'); }
  }, [scopeKey, threadId]);
  function updateScope(nextSupplier: string, nextPeriod: string) {
    setSupplier(nextSupplier); setPeriod(nextPeriod);
    try { sessionStorage.setItem(scopeKey, JSON.stringify({ supplier: nextSupplier, period: nextPeriod })); setNotice(''); }
    catch { setNotice('分析范围保存失败，请记录条件后再离开。'); }
  }
  useEffect(() => { messages.current?.scrollTo({ top: 0 }); setNewMessage(false); nearBottom.current = true; previousPreview.current = 'complete'; }, [threadId]);
  useEffect(() => {
    if (previousPreview.current === preview) return;
    previousPreview.current = preview;
    if (nearBottom.current) messages.current?.scrollTo({ top: messages.current.scrollHeight });
    else setNewMessage(true);
  }, [preview]);
  useEffect(() => { if (!latest) return; if (nearBottom.current) messages.current?.scrollTo({ top: messages.current.scrollHeight }); else setNewMessage(true); }, [latest?.id, latest?.status]);
  function startNew() {
    let existing: string | null;
    try { existing = sessionStorage.getItem(`supply-draft:${currentUser}:${factory}:new`); }
    catch { setNotice('无法读取已保存草稿，请先复制当前内容。'); return; }
    if ((existing?.trim() || document.querySelector('[data-unsaved="true"]')) && !window.confirm('新建对话将清空新会话草稿，历史会话草稿仍会保留。是否继续？')) return;
    try { sessionStorage.removeItem(`supply-draft:${currentUser}:${factory}:new`); sessionStorage.removeItem(scopeKey); }
    catch { setNotice('无法清除已保存草稿，请先复制当前内容。'); return; }
    setSuggested(undefined); setSupplier(''); setPeriod(''); setNotice(''); setReset(value => value + 1); setHistoryOpen(false);
    navigate('/assistant' + scopeQuery);
  }
  function choosePrompt(text: string) {
    if (document.querySelector('[data-unsaved="true"]') && !window.confirm('用示例问题替换当前草稿？')) return;
    setSuggested({ text, context: threadId || 'new' });
  }
  if (isExample && factory === 'B') return <EmptyState title="对象不存在或不在当前工厂范围"><ScopedLink to="/assistant">返回智能助手</ScopedLink></EmptyState>;
  return <div className="sp-chat-layout">
    <aside className={`sp-conversations ${historyOpen ? 'sp-panel-open' : ''}`} aria-label="历史对话">
      <Button primary disabled={busy} className="sp-new-conversation" onClick={startNew}><Plus size={18} />新建对话</Button>
      <button className="sp-mobile-panel" aria-expanded={historyOpen} aria-controls="assistant-history" onClick={() => setHistoryOpen(!historyOpen)}>历史对话</button>
      <div id="assistant-history" className="sp-panel-body"><h2>历史对话</h2>
        {threads.map(item => <div className="sp-conversation-item" key={item.id}><ScopedLink to={`/assistant/${item.id}?factory=${item.factory}`} className="sp-conversation-link" aria-current={threadId === item.id ? 'page' : undefined}><MessageSquare size={14} /><span>{item.title}<small>{new Date(item.created_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })} · 真实</small></span></ScopedLink></div>)}
        {factory === 'B' ? !threads.length && <p className="sp-muted">当前工厂暂无对话</p> : <div className="sp-conversation-item">
          <ScopedLink to="/assistant/THREAD-001" className="sp-conversation-link" aria-current={isExample ? 'page' : undefined}><MessageSquare size={14} aria-hidden="true" /><span>P001 缺料调查 · 示例<small><time dateTime="2026-10-02T14:20:00+08:00">2026/10/02 14:20</time></small></span></ScopedLink>
          <details className="sp-conversation-more" onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) event.currentTarget.open = false; }} onKeyDown={event => { if (event.key === 'Escape') { event.currentTarget.open = false; event.currentTarget.querySelector('summary')?.focus(); } }} onClick={event => { if ((event.target as HTMLElement).closest('a')) event.currentTarget.open = false; }}>
            <summary aria-label="P001 缺料调查的更多操作" title="更多操作"><MoreHorizontal size={16} /></summary>
            <div className="sp-conversation-menu"><ScopedLink to="/assistant/THREAD-001">打开会话</ScopedLink><ScopedLink to="/risks/EVT-001?tab=overview">查看关联事件</ScopedLink></div>
          </details>
        </div>}
      </div>
    </aside>
    <section className="sp-chat" aria-label="聊天分析">
      <div className="sp-chat-topline"><span>{threadId ? '调查会话' : '新会话'}</span><button ref={scopeButton} className="sp-button sp-scope-trigger" aria-label="当前分析范围" aria-expanded={scopeOpen} aria-controls="assistant-scope" onClick={() => setScopeOpen(!scopeOpen)}><ListFilter size={14} />分析范围<ChevronDown size={13} /></button></div>
      {notice && <p role="alert">{notice}</p>}{serviceError && <div role="alert">{serviceError}<Button onClick={() => setLoadVersion(value => value + 1)}>重新载入</Button></div>}
      <div ref={messages} className={`sp-chat-messages ${!threadId ? 'sp-chat-empty' : ''}`} tabIndex={0} aria-label="分析消息" onScroll={event => { const el = event.currentTarget; nearBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 64; if (nearBottom.current) setNewMessage(false); }}>
        {threadId && !isExample ? <><h2>{thread?.title || '载入会话'}</h2><AnalysisMessages tasks={tasks} busy={busy} onFollowup={(task, text) => void followupTask(task, text)} onSelectRisk={(task, risk) => void selectRisk(task, risk)} onConfirm={(task, plan) => void taskAction(task, 'confirm', plan)} onCancel={task => void taskAction(task, 'cancel')} onRetry={task => { setSuggested({ text: task.text, context: threadId || 'new' }); updateScope(task.plan.supplier, task.plan.period); }} /></> : !threadId ? <div className="sp-chat-welcome"><div className="sp-welcome-mark"><Sparkles size={28} /></div><h2>今天想调查什么？</h2><p>描述你的问题，我会协助查询库存、核对数据并计算已接入场景的缺料风险。</p><div className="sp-prompt-cards">{promptCards.map(({ title, description, detail, Icon }, index) => <button key={title} className="sp-prompt-card" aria-label={prompts[index]} onClick={() => choosePrompt(prompts[index])}><Icon size={22} /><strong>{title}</strong><span>{description}</span><span>{detail}<ArrowUpRight size={13} /></span></button>)}</div><div className="sp-welcome-status"><span className={capabilities?.available ? 'is-ready' : ''} />{capabilities?.available ? '分析服务已连接 · 缺料计算使用已接入场景' : '当前可编辑草稿 · 分析服务暂不可用'}{factory !== 'B' && <ScopedLink to="/assistant/THREAD-001">查看示例分析</ScopedLink>}</div></div> : <>
          <h2>P001 缺料调查</h2><StatusTag>示例分析 · 非实时结果</StatusTag>
          <p className="sp-example-note">以下内容仅用于展示分析流程，不代表实际库存或已执行的处置。</p>
          <label className="sp-preview-select">示例状态预览<select aria-label="示例状态预览" value={preview} onChange={event => setPreview(event.target.value as keyof typeof states)}>{Object.entries(states).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
          <article className="sp-message sp-message-user"><strong>用户 · 示例问题</strong><p>请调查 A 工厂 P001 控制器缺料风险，并给出处置建议。</p></article>
          <article className="sp-message" aria-label="助手示例回答"><strong>智能助手 · 示例</strong><div role="status"><StatusTag tone={preview === 'failed' ? 'error' : preview === 'complete' ? 'success' : 'info'}>{states[preview]} · 示例</StatusTag></div>
            {preview === 'ready' && <p>问题与范围已就绪；真实分析需等待供应链服务接入。</p>}
            {preview === 'running' && <p>正在核对库存与需求、整理风险依据。这是状态展示，没有后台任务运行。</p>}
            {preview === 'failed' && <><p>示例失败：数据查询服务不可用。请检查数据连接；服务恢复后可重新发起调查。</p><ScopedLink to="/data/connections">检查数据连接</ScopedLink></>}
            {preview === 'empty' && <><p>示例无数据：当前范围内没有可用记录，不能据此判断没有风险。请检查时间范围与数据来源。</p><ScopedLink to="/data/catalog">查看数据目录</ScopedLink></>}
            {preview === 'complete' && <><h3>结论</h3><p>P001 存在缺料风险，需要进一步确认可用库存与到货承诺。</p><h3>依据</h3><p>来源：示例事件 EVT-001、示例库存快照 v1。此示例未执行实时查询。</p><h3>建议</h3><p>核对需求及到货时间，评估跨厂调拨 400 件的示例方案；方案需审批，尚未执行。</p><ScopedLink to="/risks/EVT-001?tab=overview">查看关联事件 EVT-001<ArrowRight size={15} /></ScopedLink></>}
          </article>
        </>}
      </div>
      {newMessage && <Button onClick={() => { messages.current?.scrollTo({ top: messages.current.scrollHeight, behavior: 'smooth' }); setNewMessage(false); }}>有新消息 · 查看</Button>}
      <Composer key={threadId || 'new'} contextKey={threadId || 'new'} suggested={suggested?.context === (threadId || 'new') ? suggested : undefined} reset={reset} available={!!capabilities?.available && !isExample && (!threadId || !!thread)} busy={busy || pending} onSend={sendMessage} toolbar={<div className="sp-composer-tools"><ScopedLink to="/data/connections" className="sp-add-data" aria-label="前往数据接入" title="前往数据接入"><Plus size={20} /></ScopedLink><label className="sp-scope-chip">工厂：<select aria-label="草稿工厂" value={factory} disabled={busy} onChange={event => { const value = event.target.value; if (document.querySelector('[data-unsaved="true"]') && !window.confirm('当前草稿会保留在原工厂。是否切换工厂？')) return; navigate(`/assistant?factory=${value}`); }}><option value="all">全部</option><option value="A">A 工厂</option><option value="B">B 工厂</option></select></label><label className="sp-scope-chip">时间：<select aria-label="输入区时间范围" disabled={!!threadId || busy} value={threadId ? latest?.plan.period || '' : period} onChange={event => updateScope(supplier, event.target.value)}><option value="">待确认</option><option>未来三天</option><option>未来一周</option><option>未来一个月</option></select></label></div>} />
    <aside ref={scopePanel} hidden={!scopeOpen} className="sp-analysis-scope sp-scope-popover" aria-label="当前分析范围">
      <div id="assistant-scope" className="sp-panel-body"><h2>{threadId ? '当次分析范围' : '当前分析范围'}</h2>
        {threadId && !isExample ? <><p className="sp-muted">{latest?.status === 'awaiting_confirmation' ? '候选范围，等待澄清' : '当次执行范围'}</p>{latest && <ScopeFacts plan={latest.result?.scope || latest.plan} />}<p className="sp-muted">库存与质量查询使用账号导入记录；缺料计算使用当次确认的数据库场景。每条回答保留独立范围与证据。</p></> : threadId ? <><p className="sp-muted">示例固定范围，修改全局筛选不会改写历史结论。</p><dl><div><dt>工厂</dt><dd>A 工厂</dd></div><div><dt>供应商</dt><dd>未限定</dd></div><div><dt>时间范围</dt><dd>库存快照 v1 时点</dd></div><div><dt>数据来源</dt><dd>示例库存快照 v1</dd></div></dl></> : <><p className="sp-muted">明确的只读查询直接执行；存在遗漏、冲突或不支持的条件时会追问。</p><label>供应商<input aria-label="分析供应商" value={supplier} onChange={event => updateScope(event.target.value, period)} placeholder="未限定" /></label><label>时间范围<select aria-label="分析时间范围" value={period} onChange={event => updateScope(supplier, event.target.value)}><option value="">未限定</option><option>未来三天</option><option>未来一周</option><option>未来一个月</option></select></label><p className="sp-muted">工厂沿用顶栏选择。供应商筛选尚不支持；模拟缺料计算从场景起点开始，最长 14 天。</p><dl><div><dt>数据来源</dt><dd>{capabilities?.available ? '文件导入记录 / 已接入的模拟数据库' : '服务不可用，未读取数据'}</dd></div></dl><ScopedLink to="/data/connections">查看数据接入状态</ScopedLink></>}
      </div>
    </aside>
    </section>
  </div>;
}
