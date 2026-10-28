import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { PageHeading } from '../supply/components';
import { request } from './api';
import ApprovalDetail from './ApprovalDetail';

type Item = { run_id: string; proposal_version: number; status: string; title: string; requested_by: string;
  scope: { business_as_of: string; region_ids: string[] } };
const states: Record<string, string> = { pending: '待审核', approved: '已批准', rejected: '已驳回', invalidated: '已失效' };

export default function Approvals() {
  const [value, setValue] = useState<{ rows: Item[]; total_rows: number } | null>(null);
  const [page, setPage] = useState(1), [search, setSearch] = useState(''), [error, setError] = useState('');
  const [view, setView] = useState('pending'), [selected, setSelected] = useState<Item | null>(null);
  const { approvalId } = useParams();
  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    setValue(null);
    async function load() {
      try {
        const data = await request<{ rows: Item[]; total_rows: number }>(`/approvals?${new URLSearchParams({ page: String(page), view, search })}`, undefined, controller.signal);
        if (!controller.signal.aborted) { setValue(data); setError(''); }
      } catch (cause) { if (!controller.signal.aborted) { setValue(null); setError((cause as Error).message); } }
      finally { if (!controller.signal.aborted) timer = setTimeout(load, 2000); }
    }
    void load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [page, view, search]);
  return <><PageHeading title="审批中心" subtitle="物流内部核查建议 · 跨账号审核" />
    {approvalId ? <><Link className="sp-button" to="/approvals">返回审批列表</Link><ApprovalDetail /></> : <div className="lg-approval-layout"><section className="sp-section lg-approval-list"><h2>建议版本列表</h2><p>当前授权建议；审核在本页完成，批准不等于执行。</p>
      <nav className="sp-tabs" aria-label="审批筛选"><button className={view === 'pending' ? 'is-active' : ''} onClick={() => { setView('pending'); setPage(1); setSelected(null); }}>待审核</button><button className={view === 'history' ? 'is-active' : ''} onClick={() => { setView('history'); setPage(1); setSelected(null); }}>审批历史</button></nav>
      <label>搜索授权建议<input aria-label="搜索记录" value={search} maxLength={200} onChange={event => { setSearch(event.target.value); setPage(1); }} /></label>
      {error && <p role="alert">{error}</p>}
      <div className="sp-table-scroll"><table className="sp-table"><caption className="sr-only">物流建议审核列表</caption>
        <thead><tr>{['运行与版本', '建议', '范围与时点', '状态', '发起人', '操作'].map(label => <th key={label}>{label}</th>)}</tr></thead>
        <tbody>{value?.rows.map(item => <tr key={`${item.run_id}:${item.proposal_version}`}>
          <td>提交 v{item.proposal_version}</td><td>{item.title}</td><td>区域 {item.scope.region_ids.join('、')} · {item.scope.business_as_of}</td>
          <td>{states[item.status]}</td><td>{item.requested_by}</td><td><button className="sp-button" onClick={() => setSelected(item)}>核对建议与证据</button><Link to={`/approvals/${item.run_id}?version=${item.proposal_version}`}>打开详情</Link></td>
        </tr>)}{(!value || !value.rows.length) && <tr><td colSpan={6}>{error ? '读取失败' : value ? '暂无业务记录' : '正在读取建议…'}</td></tr>}</tbody>
      </table></div>
      <div className="sp-pagination"><span>{value?.total_rows ?? '—'} 条 · 第 {page} 页</span>
        <button className="sp-button" disabled={page <= 1} onClick={() => setPage(value => value-1)}>上一页</button>
        <button className="sp-button" disabled={!value || page*20 >= value.total_rows} onClick={() => setPage(value => value+1)}>下一页</button>
      </div>
    </section><ApprovalDetail selectedRun={selected?.run_id || value?.rows[0]?.run_id} selectedVersion={selected?.proposal_version || value?.rows[0]?.proposal_version} /></div>}
  </>;
}
