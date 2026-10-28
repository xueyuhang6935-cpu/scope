import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { request } from './api';

type Todo = { run_id: string; conversation_id?: string | null; title: string; status: string; scope: { region_ids: string[]; business_as_of: string } };
type Todos = { categories: { kind: string; name: string; count: number }[]; rows: Todo[]; kind: string; total_rows: number; scope_label: string };
const states: Record<string, string> = { pending: '待审核', approved: '已批准', rejected: '已驳回', invalidated: '已失效', changes_requested: '需修改', queued: '排队中', running: '调查中', waiting: '等待处理', completed: '已完成', failed: '失败', cancelled: '已取消' };

export default function WorkflowOverview() {
  const [todos, setTodos] = useState<Todos | null>(null);
  const [kind, setKind] = useState('needs_changes'), [page, setPage] = useState(1), [revision, setRevision] = useState(0);
  const [opening, setOpening] = useState(false);
  const navigate = useNavigate();
  const [todoError, setTodoError] = useState('');
  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    setTodos(null); setTodoError('');
    async function load() {
      try { const value = await request<Todos>(`/approvals/todos?${new URLSearchParams({ kind, page: String(page), page_size: '5' })}`, undefined, controller.signal); if (!controller.signal.aborted) { setTodos(value); setTodoError(''); } }
      catch (cause) { if (!controller.signal.aborted) { setTodos(null); setTodoError((cause as Error).message); } }
      finally { if (!controller.signal.aborted) timer = setTimeout(load, 10000); }
    }
    void load(); return () => { controller.abort(); clearTimeout(timer); };
  }, [kind, page, revision]);
  async function open(item: Todo) {
    setOpening(true); setTodoError('');
    try { const value = await request<{ path: string }>(`/conversations/for-run/${encodeURIComponent(item.run_id)}`, {}); navigate(value.path); }
    catch (cause) { setTodoError((cause as Error).message); }
    finally { setOpening(false); }
  }
  const categories = todos?.categories.filter(item => ['needs_changes', 'awaiting_receipt', 'awaiting_review'].includes(item.kind)) || [];
  return <aside className="lg-right-stack" aria-label="账号待办">
    <section className="sp-section"><h2>需要处理</h2><p className="sp-muted">{todos?.scope_label || '当前账号全部授权记录，不按当前调查筛选'}</p>
      {todoError ? <p role="alert">{todoError}</p> : !todos ? <p role="status">正在读取待办…</p> : <>
        <div className="lg-todo-categories">{categories.map(item => <button className={`lg-todo-category ${todos.kind === item.kind ? 'is-active' : ''}`} aria-pressed={todos.kind === item.kind} key={item.kind} onClick={() => { setKind(item.kind); setPage(1); }}><span>{item.name}</span><strong>{item.count}</strong></button>)}</div>
        {!categories.length && <p>当前角色没有调查处理待办。</p>}
        {todos.rows.map(item => <button className="lg-overview-record" key={item.run_id} disabled={opening} onClick={() => void open(item)}><strong>{item.title}</strong><small>{states[item.status] || ({ followup: '待回执', receipt: '待复核' }[item.status]) || item.status} · 区域 {item.scope.region_ids.join('、')} · {item.scope.business_as_of}</small></button>)}
        {todos.categories.length > 0 && !todos.rows.length && <p>当前类别暂无待办。</p>}
        {todos.total_rows > 5 && <div className="lg-actions"><button className="sp-button" aria-label="待办上一页" disabled={page <= 1} onClick={() => setPage(value => value - 1)}>上一页</button><span>第 {page} 页</span><button className="sp-button" aria-label="待办下一页" disabled={page*5 >= todos.total_rows} onClick={() => setPage(value => value + 1)}>下一页</button></div>}
      </>}
    </section>
    <button className="sp-button" onClick={() => setRevision(value => value + 1)}>刷新账号工作流</button>
  </aside>;
}
