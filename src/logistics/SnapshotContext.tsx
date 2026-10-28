import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import { request } from './api';

export type SnapshotConfig = { configured: boolean; batch_id: string | null; city: string; region_ids: string[] };
export type Snapshot = { snapshot_id: string; business_as_of: string; city: string; region_ids: string[] };
export type Metric = { name: string; value: number | null; status: 'ok' | 'empty' | 'unavailable'; known_lower_bound?: number };
export type Summary = { metrics: Record<string, Metric>; limitations: string[]; status: string; missing_window_count: number;
  metric_version: string; horizon_minutes: number; recent_minutes: number; visible_tasks: number };
type SnapshotState = {
  config: SnapshotConfig | null; region: string; at: string; snapshot: Snapshot | null; summary: Summary | null;
  status: 'idle' | 'dirty' | 'creating' | 'loading' | 'ready' | 'error'; error: string;
  setRegion: (value: string) => void; setAt: (value: string) => void;
  create: (scope?: { region: string; at: string }) => Promise<void>; retrySummary: () => Promise<void>; reloadConfig: () => void; invalidate: (message: string) => void;
};
const Context = createContext<SnapshotState | null>(null);
export function useSnapshot() {
  const value = useContext(Context);
  if (!value) throw new Error('缺少快照上下文。');
  return value;
}

export function SnapshotProvider({ children }: { children: ReactNode }) {
  const [config, setConfig] = useState<SnapshotConfig | null>(null), [revision, setRevision] = useState(0);
  const [region, setRegionValue] = useState(''), [at, setAtValue] = useState('2000-07-02T10:00');
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null), [summary, setSummary] = useState<Summary | null>(null);
  const [status, setStatus] = useState<SnapshotState['status']>('idle'), [error, setError] = useState('');
  const active = useRef<AbortController | null>(null);

  function clear(next: SnapshotState['status'] = 'dirty') {
    active.current?.abort(); setSnapshot(null); setSummary(null); setError(''); setStatus(next);
  }
  useEffect(() => {
    const controller = new AbortController(); setConfig(null);
    request<SnapshotConfig>('/snapshots/config', undefined, controller.signal).then(value => {
      if (!controller.signal.aborted) { setConfig(value); setRegionValue(value.region_ids.includes('5') ? '5' : value.region_ids[0] || ''); }
    }).catch(cause => { if (!controller.signal.aborted) { setError(cause.message); setStatus('error'); } });
    return () => controller.abort();
  }, [revision]);
  useEffect(() => () => active.current?.abort(), []);

  function failed(cause: unknown, controller: AbortController) {
    if (controller.signal.aborted) return;
    const failure = cause as Error & { status?: number };
    if ([401, 403, 404, 409].includes(failure.status || 0)) setSnapshot(null);
    setSummary(null); setError(failure.message); setStatus('error');
  }
  async function readSummary(value: Snapshot, controller: AbortController) {
    const result = await request<Summary>(`/snapshots/${encodeURIComponent(value.snapshot_id)}/summary`, undefined, controller.signal);
    if (!controller.signal.aborted) { setSummary(result); setStatus('ready'); }
  }
  async function create(scope = { region, at }) {
    if (status === 'creating' || status === 'loading') return;
    clear('idle');
    if (!config?.batch_id || !scope.region || !/^2000-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$/.test(scope.at)) {
      setError('请选择授权区域和 2000 年参考时点。'); setStatus('error'); return;
    }
    setRegionValue(scope.region); setAtValue(scope.at);
    const controller = new AbortController(); active.current = controller; setStatus('creating');
    try {
      const value = await request<Snapshot>('/snapshots', { batch_id: config.batch_id, city: config.city,
        region_ids: scope.region === 'all' ? config.region_ids : [scope.region], business_as_of: `${scope.at.replace('T', ' ')}${scope.at.length === 16 ? ':00' : ''}` }, controller.signal, 120000);
      if (controller.signal.aborted) return;
      setSnapshot(value); setStatus('loading'); await readSummary(value, controller);
    } catch (cause) { failed(cause, controller); }
  }
  async function retrySummary() {
    if (!snapshot || status === 'loading') return;
    active.current?.abort(); const controller = new AbortController(); active.current = controller;
    setSummary(null); setError(''); setStatus('loading');
    try { await readSummary(snapshot, controller); } catch (cause) { failed(cause, controller); }
  }
  return <Context.Provider value={{ config, region, at, snapshot, summary, status, error,
    setRegion: value => { clear(); setRegionValue(value); }, setAt: value => { clear(); setAtValue(value); },
    create, retrySummary, reloadConfig: () => { clear('idle'); setRevision(value => value + 1); },
    invalidate: message => { clear('error'); setError(message); } }}>{children}</Context.Provider>;
}
