import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';
import { supplyRequest } from './supplyApi';

export type RoleGrant = { role: string; factories: string[]; domains: string[]; approval_limit: string };
export type AccessInfo = { account: string; workspaceId: string; workspaces: Array<{ id: string; name: string; owner_id: number }>; roles: RoleGrant[]; capabilities: Record<string, string[]>; roleCatalog: Record<string, string>; domains: string[] };
const actions: Record<string, string[]> = { planner: ['view', 'data.read', 'plan', 'risk.update', 'scan'], logistics: ['view', 'data.read', 'event.update'], approver: ['view', 'data.read', 'approve'], data_admin: ['view', 'data.read', 'data.write', 'connection'], system_admin: ['view', 'admin', 'monitor', 'policy'], viewer: ['view', 'data.read'] };
export const workspaceHeaders = (): Record<string, string> => { const w = sessionStorage.getItem('business-workspace'); return w ? { 'X-Workspace-Id': w } : {}; };
type Context = { info: AccessInfo | null; error: string; blocked: boolean; allows: (action: string, factories?: string[], domain?: string) => boolean; refresh: () => void };
const AccessContext = createContext<Context>({ info: null, error: '', blocked: false, allows: () => false, refresh: () => {} });
export const useBusinessAccess = () => useContext(AccessContext);

export function BusinessAccessProvider({ children, user }: { children: ReactNode; user: string }) {
  const [info, setInfo] = useState<AccessInfo | null>(null), [error, setError] = useState(''), [version, setVersion] = useState(0);
  const [blocked, setBlocked] = useState(false);
  useEffect(() => {
    try { if (sessionStorage.getItem('business-user') !== user) { sessionStorage.removeItem('business-workspace'); sessionStorage.setItem('business-user', user); } } catch { /* Storage can be disabled by the browser. */ }
    let stopped = false; let timer: ReturnType<typeof setTimeout>;
    async function load() {
      try { const data = await supplyRequest<AccessInfo>('/access/me'); if (!stopped) { setInfo(data); setError(''); setBlocked(false); } }
      catch (e) { if (!stopped) { setInfo(null); setError((e as Error).message); setBlocked([401, 403].includes((e as Error & { status: number }).status)); } }
      finally { if (!stopped) timer = setTimeout(load, 5000); }
    }
    void load(); return () => { stopped = true; clearTimeout(timer); };
  }, [user, version]);
  const allows = (action: string, factories: string[] = [], domain?: string) => !!info?.roles.some(g => actions[g.role]?.includes(action) && factories.every(f => g.factories.includes(f)) && (!domain || g.domains.includes(domain)));
  return <AccessContext.Provider value={{ info, error, blocked, allows, refresh: () => setVersion(v => v + 1) }}>{children}</AccessContext.Provider>;
}

export function WorkspaceSelect() {
  const { info, error } = useBusinessAccess();
  if (!info) return error ? <button className="sp-button" onClick={() => { sessionStorage.removeItem('business-workspace'); location.reload(); }}>重新载入权限</button> : null;
  return <label>工作空间 <select aria-label="工作空间" value={info.workspaceId} style={{ maxWidth: 160 }} onChange={e => { sessionStorage.setItem('business-workspace', e.target.value); location.assign('/workspace'); }}>{info.workspaces.map(w => <option value={w.id} key={w.id}>{w.name} · {w.id.slice(0, 8)}</option>)}</select></label>;
}
