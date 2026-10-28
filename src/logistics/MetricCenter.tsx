import { useState } from 'react';
import { metricDefinitions, type MetricDefinition } from './metricDefinitions';
import MetricDetails, { MetricCard } from './MetricDetails';
import { useHomeMetrics } from './HomeMetrics';

export function MetricCatalog({ onSelect }: { onSelect?: (definition: MetricDefinition) => void }) {
  const [category, setCategory] = useState('全部'), [search, setSearch] = useState('');
  const [details, setDetails] = useState<MetricDefinition | null>(null);
  const { ids, save, error } = useHomeMetrics();
  const shown = metricDefinitions.filter(item => (category === '全部' || item.category === category) && `${item.name} ${item.description}`.includes(search.trim()));
  return <div className="lg-metric-center">
    <section className="sp-section"><div className="lg-catalog-tools"><nav className="sp-tabs" aria-label="指标分类">{['全部', ...new Set(metricDefinitions.map(item => item.category))].map(value => <button key={value} className={category === value ? 'is-active' : ''} aria-pressed={category === value} onClick={() => setCategory(value)}>{value}</button>)}</nav><label>搜索指标<input value={search} onChange={event => setSearch(event.target.value)} placeholder="搜索名称或说明" /></label></div>
      <p className="sp-muted">共 {shown.length} 项已接通指标 · 在风险中心查看任务与专题</p>
      {error && <p role="alert">{error}</p>}
      <div className="lg-catalog-grid">{shown.map(item => <article key={item.id} className="lg-catalog-item"><MetricCard definition={item} onClick={() => setDetails(item)} /><p>{item.description}</p><div className="lg-actions"><span className="lg-status">{item.category}</span><button className="sp-button" onClick={() => setDetails(item)}>查看{item.name}口径</button>{onSelect && <button className="sp-button" onClick={() => onSelect(item)}>查看{item.name}明细</button>}<button className="sp-button" disabled={ids.includes(item.id) && ids.length === 1} onClick={() => save(ids.includes(item.id) ? ids.filter(id => id !== item.id) : [...ids, item.id])}>{ids.includes(item.id) ? `移出展示：${item.name}` : `加入展示：${item.name}`}</button></div></article>)}</div>
      {!shown.length && <p role="status">没有匹配的指标，请调整分类或搜索条件。</p>}
    </section><MetricDetails definition={details} onClose={() => setDetails(null)} />
  </div>;
}
