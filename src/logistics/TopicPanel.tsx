import { useEffect, useRef, useState } from 'react';
import { request } from './api';
import { useSnapshot, type Metric } from './SnapshotContext';
import TaskEvidence, { type EvidenceTask } from './TaskEvidence';
import { metricDefinitions } from './metricDefinitions';

export type Topic = 'couriers' | 'aois' | 'quality' | 'recent_accepted' | 'recent_completed';
type Group = { courier_id?: string; aoi_id?: string; metrics: Record<string, Metric & { share: number | null }> };
type Quality = { available: number; unavailable: number; denominator: number; not_applicable: number; available_share: number | null };
type FlowTask = EvidenceTask & { row_index: number };
type Result = { rows?: (FlowTask | Group)[]; quality?: Record<string, Quality>; total_rows: number; status: string; limitations: string[] };
const titles = { couriers: '人员负荷', aois: 'AOI 风险分布', quality: '可见字段质量', recent_accepted: '近期接单明细', recent_completed: '近期完成明细' };
const qualityNames: Record<string, string> = { window: '窗口', stop_coordinates: '任务位置', accept_gps: '接单 GPS', pickup_gps: '完成 GPS' };
const amount = (metric: Metric) => metric.status === 'unavailable' ? `不可计算（已知至少 ${metric.known_lower_bound ?? 0}）` : metric.status === 'empty' ? '空样本' : metric.value;

export default function TopicPanel({ topic, onClose }: { topic: Topic | null; onClose: () => void }) {
  const { snapshot, summary, invalidate } = useSnapshot();
  const dialog = useRef<HTMLDialogElement>(null);
  const [value, setValue] = useState<Result | null>(null);
  const [error, setError] = useState(''), [page, setPage] = useState(1), [search, setSearch] = useState(''), [revision, setRevision] = useState(0);
  const [evidenceIndex, setEvidenceIndex] = useState<number | null>(null);
  useEffect(() => { setPage(1); setSearch(''); setEvidenceIndex(null); }, [topic, snapshot?.snapshot_id]);
  useEffect(() => {
    setValue(null); setError('');
    if (!topic || !snapshot) { dialog.current?.close(); return; }
    dialog.current?.showModal();
    const controller = new AbortController();
    const path = topic === 'quality' ? 'quality' : topic === 'couriers' || topic === 'aois' ? `groups?dimension=${topic === 'couriers' ? 'courier_id' : 'aoi_id'}&page=${page}&page_size=20` : `recent?${new URLSearchParams({ flow: topic, page: String(page), page_size: '20', search })}`;
    request<Result>(`/snapshots/${encodeURIComponent(snapshot.snapshot_id)}/${path}`, undefined, controller.signal)
      .then(data => { if (!controller.signal.aborted) setValue(data); })
      .catch(cause => { if (!controller.signal.aborted) { if ([401, 403, 404, 409].includes(cause.status)) invalidate(cause.message); else setError(cause.message); } });
    return () => controller.abort();
  }, [topic, snapshot, page, search, revision]);
  const definition = metricDefinitions.find(item => item.id === topic);
  const groups = value?.rows?.filter((row): row is Group => 'metrics' in row) || [];
  const tasks = value?.rows?.filter((row): row is FlowTask => 'order_id' in row) || [];
  return <><dialog ref={dialog} className="lg-dialog lg-topic-dialog" aria-labelledby="topic-title" onCancel={onClose} onClose={onClose}>
    <h2 id="topic-title">{topic ? titles[topic] : '业务专题'}</h2>
    <p>当前快照：区域 {snapshot?.region_ids.join('、')} · {snapshot?.business_as_of}</p>
    {definition && <p>{definition.description} 统计窗口：{summary?.recent_minutes} 分钟。</p>}
    {(topic === 'couriers' || topic === 'aois') && <p>只表达当前样本的待揽收任务与风险分布，不代表真实人员容量、责任或因果。</p>}
    {topic?.startsWith('recent_') && <label className="lg-topic-search">搜索窗口内全部授权任务<input value={search} maxLength={200} onChange={event => { setSearch(event.target.value); setPage(1); }} placeholder="任务、快递员或 AOI 编号" /></label>}
    {error ? <div role="alert"><p>{error}</p><button className="sp-button" onClick={() => setRevision(value => value + 1)}>重试读取专题</button></div> : !value ? <p role="status">正在读取专题…</p> : <>
      {value.status === 'empty' && <p>当前样本或筛选结果为空，不能推断整个区域没有风险。</p>}
      <div className="sp-table-scroll" tabIndex={0} aria-label="专题明细"><table className="sp-table"><caption className="sr-only">{topic ? titles[topic] : '业务专题'}</caption>
        {topic === 'quality' ? <><thead><tr>{['字段', '可用', '不可用', '适用分母', '不适用', '可用比例'].map(label => <th key={label}>{label}</th>)}</tr></thead><tbody>{Object.entries(value.quality || {}).map(([field, entry]) => <tr key={field}><td>{qualityNames[field]}</td><td>{entry.available}</td><td>{entry.unavailable}</td><td>{entry.denominator}</td><td>{entry.not_applicable}</td><td>{entry.available_share == null ? '不适用' : `${(entry.available_share*100).toFixed(1)}%`}</td></tr>)}</tbody></>
        : topic === 'couriers' || topic === 'aois' ? <><thead><tr>{[topic === 'couriers' ? '快递员' : 'AOI', '样本内积压', '逾期未完成', '临期未完成', '积压占比'].map(label => <th key={label}>{label}</th>)}</tr></thead><tbody>{groups.map(group => <tr key={group.courier_id || group.aoi_id}><td>{group.courier_id || group.aoi_id}</td><td>{amount(group.metrics.backlog)}</td><td>{amount(group.metrics.overdue)}</td><td>{amount(group.metrics.due_soon)}</td><td>{group.metrics.backlog.share == null ? '不可计算' : `${(group.metrics.backlog.share*100).toFixed(1)}%`}</td></tr>)}</tbody></>
        : <><thead><tr>{['任务', '快递员／AOI', '接单时点', '可见完成时点', '操作'].map(label => <th key={label}>{label}</th>)}</tr></thead><tbody>{tasks.map(row => <tr key={row.row_index}><td>{row.order_id}</td><td>{row.courier_id} / {row.aoi_id}</td><td>{row.accept_time_reference}</td><td>{row.completion_observed ? row.pickup_time_reference : '截至当前时点未观察完成'}</td><td><button className="sp-button" onClick={() => setEvidenceIndex(row.row_index)}>查看证据</button></td></tr>)}</tbody></>}
      </table></div>
      {topic !== 'quality' && <div className="sp-pagination"><span>{value.total_rows} 条 · 第 {page} 页</span><button className="sp-button" disabled={page <= 1} onClick={() => setPage(value => value - 1)}>上一页</button><button className="sp-button" disabled={page*20 >= value.total_rows} onClick={() => setPage(value => value + 1)}>下一页</button></div>}
      <details><summary>数据限制</summary>{value.limitations.map(text => <p key={text}>{text}</p>)}</details>
    </>}
    <button className="sp-button sp-primary" onClick={onClose}>关闭专题</button>
  </dialog><TaskEvidence rowIndex={evidenceIndex} onClose={() => setEvidenceIndex(null)} /></>;
}
