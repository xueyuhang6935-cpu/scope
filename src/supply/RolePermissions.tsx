import { useEffect, useState } from 'react';
import { Button, DataTable } from './components';
import { useBusinessAccess, type RoleGrant } from './BusinessAccess';
import { supplyRequest } from './supplyApi';

type Member = { account: string; enabled: number; version: number; roles: RoleGrant[] };
type Audit = { id: number; actor: string; account: string; created_at: string; before_json: string; after_json: string };
const domainLabels: Record<string, string> = { inventory: '库存', purchase: '采购', production: '生产', quality: '质量', rules: '规则' };
const roleTasks: Record<string, string> = { planner: '调查、试算、处理风险', logistics: '更新自己负责事件的交期与业务信息', approver: '按工厂和额度审批，禁止自批', data_admin: '管理授权数据域、导入和质量规则', system_admin: '管理成员、监测及审批规则；不自动具备审批权', viewer: '仅查看授权数据' };

export default function RolePermissions() {
  const { info, error: accessError, allows, refresh } = useBusinessAccess();
  const [members, setMembers] = useState<Member[]>([]), [audit, setAudit] = useState<Audit[]>([]), [query, setQuery] = useState(''), [account, setAccount] = useState(''), [enabled, setEnabled] = useState(true), [version, setVersion] = useState(0), [roles, setRoles] = useState<RoleGrant[]>([]), [message, setMessage] = useState(''), [busy, setBusy] = useState(false), [revision, setRevision] = useState(0);
  const admin = allows('admin', ['A', 'B']);
  useEffect(() => {
    if (!admin || !info) return;
    const c = new AbortController();
    Promise.all([supplyRequest<Member[]>('/access/members', undefined, c.signal), supplyRequest<Audit[]>('/access/audit', undefined, c.signal)]).then(([m, a]) => { if (!c.signal.aborted) { setMembers(m); setAudit(a); } }).catch(e => { if (!c.signal.aborted) setMessage(e.message); });
    return () => c.abort();
  }, [admin, info?.workspaceId, revision]);
  function edit(m?: Member) { setAccount(m?.account || ''); setEnabled(m ? !!m.enabled : true); setVersion(m?.version || 0); setRoles(m?.roles.map(g => ({ ...g, factories: [...g.factories], domains: [...g.domains] })) || []); setMessage(''); }
  function update(role: string, value: Partial<RoleGrant>) { setRoles(gs => gs.map(g => g.role === role ? { ...g, ...value } : g)); }
  async function save() {
    setBusy(true); setMessage('');
    try { await supplyRequest('/access/members', { account: account.trim(), enabled, version, roles }); setRevision(v => v + 1); refresh(); edit(); setMessage('成员权限已保存，即时生效。'); }
    catch (e) { setMessage((e as Error).message); } finally { setBusy(false); }
  }
  if (!info) return <p role="alert">{accessError || '正在读取权限…'}</p>;
  return <div className="sp-role-permissions"><h2>角色与权限</h2><p>当前账号：{info.account} · 工作空间：{info.workspaces.find(w => w.id === info.workspaceId)?.name}</p>
    <DataTable caption="角色权限说明" headers={['角色', '主要权限', '我的范围']}>{Object.entries(info.roleCatalog).map(([key, label]) => <tr key={key}><td>{label}</td><td>{roleTasks[key]}</td><td>{info.roles.find(g => g.role === key)?.factories.join('、') || '未授予'}</td></tr>)}</DataTable>
    {!admin ? <p>当前账号无成员管理权限。请联系该工作空间的系统管理员。</p> : <>
      <div className="sp-section-heading"><h3>成员管理</h3><Button onClick={() => edit()}>新增成员</Button></div><label>搜索成员<input aria-label="搜索成员" value={query} onChange={e => setQuery(e.target.value)} /></label>
      <DataTable caption="工作空间成员" headers={['账号 / 状态', '角色与范围', '操作']}>{members.filter(m => m.account.includes(query)).map(m => <tr key={m.account}><td>{m.account}<small>{m.enabled ? '启用' : '停用'} · v{m.version}</small></td><td>{m.roles.map(g => <div key={g.role}>{info.roleCatalog[g.role]} · {g.factories.join('、')} · {g.domains.map(d => domainLabels[d] || d).join('、')}{g.role === 'approver' ? ` · 额度 ${g.approval_limit} 元` : ''}</div>)}</td><td><Button onClick={() => edit(m)}>编辑 {m.account}</Button></td></tr>)}</DataTable>
      <section className="sp-section" aria-label="成员授权编辑"><h3>{version ? '编辑成员权限' : '授权已注册账号'}</h3><label>成员账号<input aria-label="成员账号" value={account} disabled={version > 0} onChange={e => setAccount(e.target.value)} /></label><label className="sp-role-check"><input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} />启用成员</label>
        {Object.entries(info.roleCatalog).map(([key, label]) => { const g = roles.find(r => r.role === key); return <fieldset key={key} style={{ margin: '12px 0', padding: 12 }}><legend><label className="sp-role-check"><input type="checkbox" aria-label={`授予${label}`} checked={!!g} onChange={e => setRoles(gs => e.target.checked ? [...gs, { role: key, factories: ['A', 'B'], domains: [...info.domains], approval_limit: '0' }] : gs.filter(r => r.role !== key))} />{label}</label></legend>{g && <div className="sp-form-grid"><div>工厂范围{['A', 'B'].map(f => <label className="sp-role-check" key={f}><input type="checkbox" aria-label={`${label}-${f}厂`} checked={g.factories.includes(f)} disabled={key === 'system_admin'} onChange={e => update(key, { factories: e.target.checked ? [...g.factories, f] : g.factories.filter(x => x !== f) })} />{f} 厂</label>)}</div><div>数据域{info.domains.map(d => <label className="sp-role-check" key={d}><input type="checkbox" aria-label={`${label}-${d}`} checked={g.domains.includes(d)} onChange={e => update(key, { domains: e.target.checked ? [...g.domains, d] : g.domains.filter(x => x !== d) })} />{{ inventory: '库存', purchase: '采购', production: '生产', quality: '质量', rules: '规则' }[d] || d}</label>)}</div>{key === 'approver' && <label>审批额度（元）<input type="number" aria-label="成员审批额度" min={0} max={1000000} value={g.approval_limit} onChange={e => update(key, { approval_limit: e.target.value })} /></label>}</div>}</fieldset>; })}
        <p>取消角色勾选可撤销该角色；停用成员会立即阻止其访问。系统管理员不能给自己新增审批权限，最后一位系统管理员不能被撤销。</p><Button primary disabled={busy || !account.trim()} onClick={() => void save()}>保存成员权限</Button>
      </section><h3>权限与规则变更记录</h3>{audit.map(a => <details key={a.id}><summary>{new Date(a.created_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })} · {a.actor} → {a.account}</summary><pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>变更前：{a.before_json}{'\n'}变更后：{a.after_json}</pre></details>)}
    </>}{message && <p role="status">{message}</p>}</div>;
}

export function ApprovalPolicies() {
  const { allows } = useBusinessAccess();
  const [rows, setRows] = useState<Array<{ action: string; max_quantity: string; max_cost: string; version: number }>>([]), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  useEffect(() => { if (allows('policy')) supplyRequest<typeof rows>('/access/policies').then(result => setRows(['transfer', 'expedite'].map(action => result.find(p => p.action === action) || { action, max_quantity: '1000', max_cost: '10000', version: 0 }))).catch(e => setError(e.message)); }, [allows('policy')]);
  if (!allows('policy')) return <p>仅系统管理员可以配置审批规则。</p>;
  async function save(index: number) { setBusy(true); try { const r = rows[index]; await supplyRequest('/access/policies', { action: r.action, maxQuantity: r.max_quantity, maxCost: r.max_cost, version: r.version }); const saved = await supplyRequest<typeof rows>('/access/policies'); setRows(previous => previous.map(p => saved.find(s => s.action === p.action) || p)); setError('审批规则已保存，旧方案需重新核对。'); } catch (e) { setError((e as Error).message); } finally { setBusy(false); } }
  return <><h2>审批规则</h2><p>工作空间规则和场景原始规则同时校验，采用更严格的限制。</p>{rows.map((r, i) => <fieldset key={r.action}><legend>{r.action === 'transfer' ? '调拨' : '加急'} · v{r.version}</legend><label>数量上限<input type="number" value={r.max_quantity} onChange={e => setRows(rs => rs.map((p, n) => n === i ? { ...p, max_quantity: e.target.value } : p))} /></label><label>费用上限<input type="number" value={r.max_cost} onChange={e => setRows(rs => rs.map((p, n) => n === i ? { ...p, max_cost: e.target.value } : p))} /></label><Button disabled={busy} onClick={() => void save(i)}>保存{r.action === 'transfer' ? '调拨' : '加急'}规则</Button></fieldset>)}{error && <p role="status">{error}</p>}</>;
}
