import { createContext, useContext, useEffect, useState } from 'react';
import { Screen } from '../types';
import { request, type Access } from './api';
import './logistics.css';
import SupplyShell from '../supply/SupplyShell';
import { SnapshotProvider } from './SnapshotContext';
import HomeMetricsProvider from './HomeMetrics';

const Context = createContext<{ access: Access | null; refresh: () => void }>({ access: null, refresh: () => {} });
export const useAccess = () => useContext(Context);
export default function LogisticsShell({ currentUser, onNavigate }: { currentUser: string; onNavigate: (screen: Screen) => void }) {
  const [access, setAccess] = useState<Access | null>(null), [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const [loadedFor, setLoadedFor] = useState('');
  useEffect(() => {
    try { sessionStorage.removeItem('business-workspace'); sessionStorage.removeItem('business-user'); } catch { /* 无本地存储也可使用。 */ }
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    async function load() {
      try { const value = await request<Access>('/access/me', undefined, controller.signal); if (!controller.signal.aborted) { setAccess(value); setLoadedFor(currentUser); setError(''); } }
      catch (cause) { if (!controller.signal.aborted) { setAccess(null); setError((cause as Error).message); } }
      finally { if (!controller.signal.aborted) timer = setTimeout(load, 5000); }
    }
    void load(); return () => { controller.abort(); clearTimeout(timer); };
  }, [currentUser, revision]);
  return <Context.Provider value={{ access, refresh: () => setRevision(v => v + 1) }}>
    {error ? <div className="session-state" role="alert"><p>{error}</p><button className="sp-button" onClick={() => setRevision(v => v + 1)}>重新加载权限</button></div> : !access || loadedFor !== currentUser ? <div className="session-state" role="status">正在加载权限…</div> : <SnapshotProvider key={`${access.workspaceId}:${access.account}:${access.version}`}><HomeMetricsProvider><SupplyShell currentUser={currentUser} onNavigate={onNavigate} /></HomeMetricsProvider></SnapshotProvider>}
  </Context.Provider>;
}
