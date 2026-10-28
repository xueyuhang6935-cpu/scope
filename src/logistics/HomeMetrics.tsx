import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import { useAccess } from './LogisticsShell';
import { defaultMetricIds, metricDefinitions, type MetricId } from './metricDefinitions';

type Preferences = { ids: MetricId[]; error: string; save: (ids: MetricId[]) => boolean; open: () => void };
const Context = createContext<Preferences | null>(null);
export function useHomeMetrics() {
  const value = useContext(Context);
  if (!value) throw new Error('缺少展示指标配置。');
  return value;
}
function validIds(value: unknown): MetricId[] {
  return Array.isArray(value) ? [...new Set(value.filter((id): id is MetricId => metricDefinitions.some(item => item.id === id)))] : [];
}

export default function HomeMetricsProvider({ children }: { children: ReactNode }) {
  const { access } = useAccess();
  const key = `scope:home-metrics:${access?.workspaceId || 'shanghai-logistics'}:${access?.account}`;
  const [error, setError] = useState('');
  const [ids, setIds] = useState<MetricId[]>(() => {
    try { const saved = validIds(JSON.parse(localStorage.getItem(key) || 'null')); return saved.length ? saved : defaultMetricIds; }
    catch { return defaultMetricIds; }
  });
  const [editing, setEditing] = useState(false), [draft, setDraft] = useState<MetricId[]>(ids);
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { if (editing) dialog.current?.showModal(); else dialog.current?.close(); }, [editing]);
  function save(value: MetricId[]) {
    const next = validIds(value);
    if (!next.length) { setError('至少保留一项展示指标。'); return false; }
    try { localStorage.setItem(key, JSON.stringify(next)); }
    catch { setError('浏览器未能保存配置，请检查存储权限后重试。'); return false; }
    setIds(next); setError(''); return true;
  }
  function move(index: number, direction: number) {
    const next = [...draft]; [next[index], next[index + direction]] = [next[index + direction], next[index]]; setDraft(next);
  }
  return <Context.Provider value={{ ids, error, save, open: () => { setDraft([...ids]); setError(''); setEditing(true); } }}>
    {children}
    <dialog ref={dialog} className="lg-dialog" aria-labelledby="home-metric-title" onCancel={() => setEditing(false)} onClose={() => setEditing(false)}>
      <h2 id="home-metric-title">配置展示指标</h2><p>至少保留一项。风险中心按所选顺序展示。仅保存当前浏览器、账号和空间的设置。</p>
      <fieldset className="lg-metric-options"><legend>选择指标</legend>{metricDefinitions.map(item => <label key={item.id}><input type="checkbox" checked={draft.includes(item.id)} onChange={event => { setError(''); setDraft(event.target.checked ? [...draft, item.id] : draft.filter(id => id !== item.id)); }} />{item.name}<small>{item.category}</small></label>)}</fieldset>
      <h3>展示顺序</h3><ol className="lg-metric-order">{draft.map((id, index) => <li key={id}><span>{metricDefinitions.find(item => item.id === id)!.name}</span><div className="lg-actions"><button className="sp-button" aria-label={`${id} 上移`} disabled={index === 0} onClick={() => move(index, -1)}>上移</button><button className="sp-button" aria-label={`${id} 下移`} disabled={index === draft.length - 1} onClick={() => move(index, 1)}>下移</button></div></li>)}</ol>
      {error && <p role="alert">{error}</p>}
      <div className="lg-actions"><button className="sp-button" onClick={() => { setDraft([...defaultMetricIds]); setError(''); }}>恢复默认</button><button className="sp-button" onClick={() => setEditing(false)}>取消</button><button className="sp-button sp-primary" disabled={!draft.length} onClick={() => { if (save(draft)) setEditing(false); }}>保存配置</button></div>
    </dialog>
  </Context.Provider>;
}
