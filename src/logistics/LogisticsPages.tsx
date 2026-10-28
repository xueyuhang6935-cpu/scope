import { useEffect, useState } from 'react';
import { Link, Navigate, useLocation } from 'react-router-dom';
import { request, type Grant, type Member } from './api';
import { useAccess } from './LogisticsShell';
import Assistant from './Assistant';
import RiskCenter from './RiskCenter';
import LegacyRun from './LegacyRun';
import Knowledge from './Knowledge';
import KnowledgeDetail from './KnowledgeDetail';
import Approvals from './Approvals';

function Heading({ title, children }: { title: string; children?: React.ReactNode }) { return <div className="sp-heading"><div><h1>{title}</h1>{children && <p>{children}</p>}</div></div>; }

export default function LogisticsPages({ notFound = false }: { notFound?: boolean }) {
  const { access } = useAccess(); const path = useLocation().pathname;
  if (notFound) return <Heading title="页面不存在"><Link to="/assistant">返回智能助手</Link></Heading>;
  if (path === '/data') return <Navigate to="/data/connections" replace />;
  if (path.startsWith('/assistant')) return <Assistant key={`${access?.account}:${access?.version}`} />;
  if (path === '/workspace') return <Navigate to={new URLSearchParams(window.location.search).has('risk') || new URLSearchParams(window.location.search).has('topic') ? `/metrics${window.location.search}` : '/assistant'} replace />;
  if (path === '/metrics') return <RiskCenter key={`${access?.account}:${access?.version}`} />;
  if (path === '/runs') return <Navigate to="/knowledge" replace />;
  if (path.startsWith('/runs/')) return <LegacyRun key={`${access?.account}:${access?.version}`} />;
  if (path.startsWith('/approvals')) return <Approvals key={`${access?.account}:${access?.version}`} />;
  if (path === '/knowledge') return <Knowledge key={`${access?.account}:${access?.version}`} />;
  if (path.startsWith('/knowledge/')) return <KnowledgeDetail key={`${access?.account}:${access?.version}`} />;
  if (path === '/risks' || path.startsWith('/risks/')) return <Navigate to="/metrics" replace />;
  if (path === '/settings') return <Members />;
  if (path === '/data/catalog' || path === '/data/governance') return <DataStructure governance={path.endsWith('governance')} />;
  if (path === '/data/connections' || path === '/data/catalog' || path === '/data/governance' || path === '/data/preview/pickup_tasks') return <DataPreview />;
  return <><Heading title="页面已停用" /><section className="sp-empty"><h2>该旧业务页面已移除</h2><Link to="/assistant">返回智能助手</Link></section></>;
}

type Connection = { batchId: string; configured: boolean; regions: string[]; batches: Array<{ batch_id: string }> };
function DataStructure({ governance }: { governance: boolean }) {
  const { access } = useAccess(); const [tab,setTab] = useState('质量检查');
  return <><Heading title={governance ? '数据治理' : '数据目录'} />{!access?.roles.some(g=>g.role==='data_admin') ? <p>需要数据管理员权限。</p> : governance ? <section className="sp-section"><nav className="sp-tabs" aria-label="治理页签">{['质量检查','标准与映射','问题处理','来源与变更'].map(t=><button key={t} className={t===tab?'is-active':''} onClick={()=>setTab(t)}>{t}</button>)}</nav><h2>{tab}</h2><p>暂无治理业务记录。原始任务中的质量标记可在数据预览查看；物流治理操作尚未接入。</p><Link to="/data/preview/pickup_tasks">查看源数据与质量标记</Link></section> : <section className="sp-section"><h2>已接入的数据源</h2><table className="sp-table"><thead><tr><th>数据集</th><th>来源</th><th>用途</th><th>操作</th></tr></thead><tbody><tr><td>上海揽收任务</td><td>LaDe-P</td><td>历史源数据管理</td><td><Link to="/data/preview/pickup_tasks">查看预览</Link></td></tr></tbody></table><p>批次和授权范围以数据接入页面为准。</p></section>}</>;
}
type Preview = { rows: Record<string, unknown>[]; columns: string[]; total: number; page: number; pageSize: number; regions: string[] };
function DataPreview() {
  const { access } = useAccess();
  const permitted = access?.roles.some(g => g.role === 'data_admin');
  const full = access?.roles.some(g => g.role === 'data_admin' && access.regionCatalog.every(r => g.regions.includes(r)));
  const [connection, setConnection] = useState<Connection | null>(null), [preview, setPreview] = useState<Preview | null>(null);
  const [batch, setBatch] = useState(''), [region, setRegion] = useState(''), [order, setOrder] = useState('');
  const [query, setQuery] = useState({ region: '', order_id: '' }), [page, setPage] = useState(1), [revision, setRevision] = useState(0);
  const [error, setError] = useState(''), [busy, setBusy] = useState(false), [loading, setLoading] = useState(false);
  useEffect(() => {
    if (!permitted) return;
    const controller = new AbortController();
    request<Connection>('/data/connection', undefined, controller.signal).then(c => { setConnection(c); setBatch(c.batchId); }).catch(e => { if (!controller.signal.aborted) setError(e.message); });
    return () => controller.abort();
  }, [permitted, revision]);
  useEffect(() => {
    if (!connection || !permitted) return;
    const controller = new AbortController(); setLoading(true); setPreview(null); setError('');
    request<Preview>(`/data/preview?${new URLSearchParams({ ...query, page: String(page) })}`, undefined, controller.signal)
      .then(setPreview).catch(e => { if (!controller.signal.aborted) setError(e.message); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [connection, permitted, query, page]);
  async function save() {
    setBusy(true); setError(''); setPreview(null);
    try { const value = await request<Connection>('/data/connection', { batchId: batch }); setPage(1); setConnection(value); }
    catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  return <><Heading title="上海揽收数据管理">原始时间的真实年份未披露；2000 仅作参考时间轴。这里只展示已授权区域的完整历史记录。</Heading>
    {!permitted ? <section className="sp-empty"><h2>需要数据管理员权限</h2><p>请联系系统管理员分配数据管理角色和区域。调查员与审核人不直接读取原始历史表。</p></section> : <>
      <div className="sp-entry-grid sp-connections-grid"><section className="sp-entry sp-connection-card"><strong>文件导入</strong><p>接入新的物流数据文件。</p><button className="sp-button" disabled title="物流文件导入服务尚未接入">上传文件</button></section><section className="sp-entry sp-connection-card"><strong>数据库连接</strong><p>上海 LaDe 数据已保留，在下方查看批次和原始任务。</p></section><section className="sp-entry sp-connection-card"><strong>业务接口</strong><p>连接真实物流业务系统。</p><button className="sp-button" disabled title="业务接口配置服务尚未接入">配置接口</button></section></div>
      <section className="sp-section"><h2>数据接入</h2>{connection && <><label>导入批次<select value={batch} onChange={e => setBatch(e.target.value)} disabled={!full || busy}>{connection.batches.map(b => <option key={b.batch_id} value={b.batch_id}>{b.batch_id}</option>)}</select></label><p>可查看区域：{connection.regions.join('、')}</p>
        <button className="sp-button sp-primary" disabled={!full || busy} onClick={save}>{busy ? '保存中…' : '保存批次选择'}</button>{!full && <p>修改全空间批次需要全部区域的数据管理员授权。</p>}</>}
        <button className="sp-button" disabled={busy || loading} onClick={() => setRevision(v => v + 1)}>重新读取连接</button></section>
      {connection && <section className="sp-section"><h2>原始任务预览</h2><p>包含最终完成信息，仅用于数据管理，不能作为时点调查输入。</p>
        <form className="sp-form-grid" onSubmit={e => { e.preventDefault(); setPage(1); setQuery({ region, order_id: order.trim() }); }}><label>区域<select value={region} onChange={e => setRegion(e.target.value)}><option value="">全部授权区域</option>{connection.regions.map(r => <option key={r} value={r}>{r}</option>)}</select></label><label>任务编号<input value={order} onChange={e => setOrder(e.target.value)} maxLength={200} /></label><button className="sp-button" disabled={loading || busy}>查询任务</button></form>
        {loading && <p role="status">正在读取数据…</p>}{preview && <><p>授权范围内共 {preview.total.toLocaleString()} 条记录</p><div className="sp-table-scroll" tabIndex={0} aria-label="上海揽收任务"><table className="sp-table"><caption className="sr-only">上海揽收任务</caption><thead><tr>{preview.columns.map(c => <th scope="col" key={c}>{c}</th>)}</tr></thead><tbody>{preview.rows.map((row, i) => <tr key={i}>{preview.columns.map(c => <td key={c}>{row[c] == null ? '—' : typeof row[c] === 'object' ? JSON.stringify(row[c]) : String(row[c])}</td>)}</tr>)}</tbody></table></div>{preview.rows.length === 0 && <p>当前筛选没有记录。</p>}
          <div className="sp-pagination"><button className="sp-button" disabled={page <= 1 || loading} onClick={() => setPage(v => v - 1)}>上一页</button><span>第 {page} / {Math.max(1, Math.ceil(preview.total / preview.pageSize))} 页</span><button className="sp-button" disabled={page * preview.pageSize >= preview.total || loading} onClick={() => setPage(v => v + 1)}>下一页</button></div></>}
      </section>}{error && <p role="alert">{error}</p>}</>}
  </>;
}

function Members() {
  const { access, refresh } = useAccess(); const admin = access?.roles.some(g => g.role === 'system_admin');
  const [members, setMembers] = useState<Member[]>([]), [editing, setEditing] = useState<Member | null>(null);
  const [error, setError] = useState(''), [busy, setBusy] = useState(false), [notice, setNotice] = useState('');
  const [audit, setAudit] = useState<Array<{ id: number; actor: string; account: string; created_at: string }>>([]);
  async function load() { const [m, a] = await Promise.all([request<Member[]>('/access/members'), request<typeof audit>('/access/audit')]); setMembers(m); setAudit(a); }
  useEffect(() => { if (admin) void load().catch(e => setError(e.message)); }, [admin]);
  async function save() {
    if (!editing) return; setBusy(true); setError(''); setNotice('');
    try { await request('/access/members', { ...editing, enabled: !!editing.enabled }); setEditing(null); await load(); refresh(); setNotice('成员权限已保存。'); }
    catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  function setRole(role: string, checked: boolean) {
    if (!editing) return;
    setEditing({ ...editing, roles: checked ? [...editing.roles, { role, city: 'Shanghai', regions: role === 'system_admin' ? [] : ['5'] }] : editing.roles.filter(g => g.role !== role) });
  }
  function changeRegions(role: string, regions: string[]) { if (editing) setEditing({ ...editing, roles: editing.roles.map(g => g.role === role ? { ...g, regions } : g) }); }
  return <><Heading title="成员与角色">同一运营空间内按角色和区域授权；系统管理员不自动拥有数据权限或审核权。</Heading>
    {!admin ? <p role="alert">需要系统管理员权限。</p> : <>
      <section className="sp-section"><h2>空间成员</h2>{members.map(m => <div className="sp-section-heading" key={m.account}><div><strong>{m.account}</strong><p>{!m.enabled ? '已停用' : m.roles.length ? m.roles.map(g => access?.roleCatalog[g.role]).join('、') : '待分配角色'}</p></div><button className="sp-button" onClick={() => { setEditing(structuredClone(m)); setError(''); setNotice(''); }}>编辑 {m.account}</button></div>)}</section>
      {editing && <section className="sp-section" aria-label="编辑成员" data-unsaved="true"><h2>{editing.account}</h2><label><input type="checkbox" checked={!!editing.enabled} onChange={e => setEditing({ ...editing, enabled: e.target.checked ? 1 : 0 })} />启用成员</label>
        {Object.entries(access?.roleCatalog || {}).map(([role, label]) => { const grant: Grant | undefined = editing.roles.find(g => g.role === role); return <fieldset key={role}><legend><label><input type="checkbox" checked={!!grant} onChange={e => setRole(role, e.target.checked)} />{label}</label></legend>{grant && role !== 'system_admin' && <><label>Shanghai 区域（按住 Ctrl 可多选）<select multiple aria-label={`${label}区域`} value={grant.regions} onChange={e => changeRegions(role, Array.from(e.target.selectedOptions, o => o.value))}>{access?.regionCatalog.map(r => <option key={r} value={r}>{r}</option>)}</select></label><button className="sp-button" onClick={() => changeRegions(role, access?.regionCatalog || [])}>选择全部区域</button></>}{grant && role === 'system_admin' && <p>管理全空间成员，不附带业务区域读取权。</p>}</fieldset>; })}
        <p>审核人不能自批；自己的审核授权须由另一位系统管理员设置。</p><div className="sp-actions"><button className="sp-button sp-primary" disabled={busy} onClick={save}>保存权限</button><button className="sp-button" disabled={busy} onClick={() => setEditing(null)}>取消</button></div></section>}
      <section className="sp-section"><h2>最近授权与配置记录</h2>{audit.length ? audit.map(a => <p key={a.id}>{a.created_at} · {a.actor} → {a.account}</p>) : <p>暂无变更记录。</p>}</section>
    </>}{error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}</>;
}
