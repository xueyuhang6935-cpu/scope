import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { ArrowRight } from 'lucide-react';
import { request } from './api';
import { PageHeading } from '../supply/components';
import { useSnapshot } from './SnapshotContext';
import ScopeBar from './ScopeBar';
import MetricDetails, { MetricCard } from './MetricDetails';
import { metricDefinitions, riskNames, minutes, type MetricDefinition } from './metricDefinitions';
import { useHomeMetrics } from './HomeMetrics';
import TaskEvidence from './TaskEvidence';
import { MetricCatalog } from './MetricCenter';
import TopicPanel, { type Topic } from './TopicPanel';

type Task = { row_index: number; order_id: string; region_id: string; courier_id: string; aoi_id: string; accept_time_reference: string;
  time_window_end_reference: string | null; current_wait_minutes: number; current_overdue_minutes: number | null; risk_state: string };
type Tasks = { rows: Task[]; total_rows: number; page_size: number; status: string };

export default function RiskCenter() {
  const [params] = useSearchParams();
  const { snapshot, summary, status, invalidate } = useSnapshot();
  const { ids, open: openConfiguration } = useHomeMetrics();
  const [tasks, setTasks] = useState<Tasks | null>(null), [selected, setSelected] = useState('risk'), [page, setPage] = useState(1);
  const [search, setSearch] = useState(''), [loading, setLoading] = useState(false), [error, setError] = useState('');
  const [details, setDetails] = useState<MetricDefinition | null>(null);
  const [revision, setRevision] = useState(0);
  const [evidenceIndex, setEvidenceIndex] = useState<number | null>(null);
  const [topic, setTopic] = useState<Topic | null>(null);
  const [view, setView] = useState('overview');
  useEffect(() => {
    const risk = params.get('risk'), requestedTopic = params.get('topic');
    if (risk && Object.hasOwn(riskNames, risk)) { setSelected(risk); setPage(1); setSearch(''); }
    if (summary && ['couriers', 'quality', 'recent_accepted', 'recent_completed'].includes(requestedTopic || '')) setTopic(requestedTopic as Topic);
  }, [params, summary]);
  useEffect(() => { setPage(1); setSearch(''); setTasks(null); setError(''); setEvidenceIndex(null); }, [snapshot?.snapshot_id]);
  useEffect(() => {
    setTasks(null);
    if (!snapshot || !summary) { setLoading(false); return; }
    const controller = new AbortController(); setLoading(true); setError('');
    request<Tasks>(`/snapshots/${encodeURIComponent(snapshot.snapshot_id)}/tasks?${new URLSearchParams({ risk_state: selected, search, page: String(page), page_size: '20' })}`, undefined, controller.signal)
      .then(value => { if (!controller.signal.aborted) setTasks(value); })
      .catch(cause => { if (!controller.signal.aborted) { if ([401, 403, 404, 409].includes(cause.status)) invalidate(cause.message); else setError(cause.message); } })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [snapshot, summary, selected, search, page, revision]);

  function select(state: string) { setSelected(state); setPage(1); setSearch(''); }
  const shown = tasks?.rows || [];
  return <div className="lg-workbench">
    <PageHeading title="风险中心" breadcrumb="上海运营空间" subtitle="风险概览、提醒与任务证据 · 当前授权历史快照" />
    <ScopeBar onConfigure={openConfiguration} />
    <nav className="sp-tabs" aria-label="风险中心视图"><button className={view === 'overview' ? 'is-active' : ''} onClick={() => setView('overview')}>风险概览</button><button className={view === 'catalog' ? 'is-active' : ''} onClick={() => setView('catalog')}>指标目录</button></nav>
    {view === 'catalog' ? <MetricCatalog onSelect={(definition) => { setView('overview'); if (definition.riskState) select(definition.riskState); else setTopic(definition.id as Topic); }} /> : <>
    <div className="lg-section-heading lg-metric-heading"><div><h2>核心指标概览</h2><span>当前角色关注 · {ids.length} 项</span></div><button className="sp-button" onClick={() => setView('catalog')}>查看全部指标<ArrowRight size={15} aria-hidden="true" /></button></div>
    {ids.length > 4 && <p className="sp-muted">已选择 {ids.length} 项；可在指标目录查看完整内容。</p>}
    <div className="lg-metric-grid">{ids.map(id => { const definition = metricDefinitions.find(item => item.id === id)!; return <MetricCard key={id} definition={definition} selected={definition.riskState === selected} onClick={() => definition.riskState ? select(definition.riskState) : setTopic(id as Topic)} onDetails={() => setDetails(definition)} />; })}</div>
    <section className="sp-section lg-risk-reminders" aria-labelledby="risk-reminders-title">
      <div className="lg-section-heading"><h2 id="risk-reminders-title">风险提醒</h2><span className="sp-muted">当前授权历史样本</span></div>
      {status === 'ready' && snapshot && summary ? <>
        <p className="sp-muted">统计范围：上海 · 区域 {snapshot.region_ids.join('、')} · 参考时点 {snapshot.business_as_of}。历史样本不代表生产全量。</p>
        {summary.status === 'empty' ? <p role="status">该时点没有可见任务，不能据此判断整个区域没有风险。</p> : <div className="lg-risk-reminder-grid">
          {metricDefinitions.filter(item => item.id === 'overdue' || item.id === 'due_soon').map(definition => {
            const metric = summary.metrics[definition.id];
            const count = !metric ? '暂无数据' : metric.status === 'empty' ? '空样本' : metric.status === 'unavailable' || metric.value === null
              ? `不可计算${metric.known_lower_bound == null ? '' : `（已知至少 ${metric.known_lower_bound.toLocaleString('zh-CN')} 条）`}` : `${metric.value.toLocaleString('zh-CN')} 条`;
            return <div className="lg-risk-reminder" key={definition.id}><h3>{definition.name} <span className={`lg-status lg-${definition.tone}`}>{count}</span></h3>
              <p>{definition.id === 'overdue' ? '逾期任务已超过揽收窗口截止时点，且尚未观察到完成，可能影响及时揽收。' : `临期任务的窗口将在当前参考时点起 ${summary.horizon_minutes ?? '—'} 分钟内截止，继续等待可能超过揽收窗口。`}</p>
              <small>对应任务的区域、快递员和 AOI 可在下方任务清单中核对。</small>
            </div>;
          })}
          <div className="lg-risk-reminder"><h3>窗口缺失 <span className={`lg-status ${summary.missing_window_count > 0 ? 'lg-orange' : 'lg-gray'}`}>{summary.missing_window_count.toLocaleString('zh-CN')} 条</span></h3>
            <p>{summary.missing_window_count > 0 ? '缺少有效揽收窗口，无法完整判断逾期和临期风险；已知数量仅为下界。' : '当前快照没有窗口缺失的待揽收任务。'}</p>
            <small>窗口不可计算的任务不应视为无风险，可在下方单独筛选。</small>
          </div>
        </div>}
      </> : <p role="status">{status === 'creating' || status === 'loading' ? '正在读取当前范围的风险数据…' : status === 'error' ? '风险数据读取失败，请检查上方数据状态。' : status === 'dirty' ? '范围已修改，请更新快照后查看风险提醒。' : '创建回放快照后展示风险提醒。'}</p>}
    </section>
    <div className="lg-main-grid"><section className="sp-section lg-task-panel" id="logistics-task-list"><div className="lg-section-heading"><h2>优先任务</h2><span className="sp-muted">{riskNames[selected]}</span></div>
      <p className="sp-muted">逾期任务已超过窗口截止时点；临期任务即将到达截止时点；窗口不可计算表示信息不足。筛选后可在本页核对任务和证据。</p>
      <nav className="sp-tabs" aria-label="任务筛选">{['risk', 'all', 'overdue', 'due_soon', 'window_unavailable'].map(state => <button key={state} className={selected === state ? 'is-active' : ''} aria-pressed={selected === state} onClick={() => select(state)}>{riskNames[state]}</button>)}</nav>
      <label>搜索全部授权任务<input aria-label="搜索记录" value={search} maxLength={200} onChange={event => { setSearch(event.target.value); setPage(1); }} placeholder="任务、快递员或 AOI 编号" /></label>
      <p className="sp-muted">{tasks ? `当前快照范围共 ${tasks.total_rows.toLocaleString()} 条匹配任务；搜索与风险筛选共同作用后分页。` : '生成快照后展示授权任务。'}</p>
      {summary && summary.missing_window_count > 0 && <p className="lg-warning">存在 {summary.missing_window_count} 条窗口缺失任务，请查看“窗口不可计算”；未进入风险集合不代表无风险。</p>}
      {error && <div role="alert"><p>{error}</p><button className="sp-button" onClick={() => setRevision(value => value + 1)}>重试读取任务</button></div>}
      {loading && <p role="status">正在读取任务…</p>}
      <div className="sp-table-scroll" tabIndex={0} aria-label="回放任务清单"><table className="sp-table"><caption className="sr-only">回放任务清单</caption><thead><tr>{['任务编号', '区域／快递员／AOI', '状态', '窗口截止', '当前等待', '当前逾期', '操作'].map(label => <th scope="col" key={label}>{label}</th>)}</tr></thead><tbody>
        {shown.map(row => <tr key={row.row_index ?? row.order_id}><td>{row.order_id}<small className="lg-subtext">接单 {row.accept_time_reference}</small></td><td>区域 {row.region_id}<small className="lg-subtext">{row.courier_id} / {row.aoi_id}</small></td><td><span className={`lg-status lg-${row.risk_state === 'overdue' ? 'red' : row.risk_state === 'due_soon' ? 'orange' : 'gray'}`}>{riskNames[row.risk_state]}</span></td><td>{row.time_window_end_reference || '不可计算'}</td><td>{minutes(row.current_wait_minutes)}</td><td>{minutes(row.current_overdue_minutes)}</td><td><button className="sp-button" disabled={row.row_index == null} onClick={() => setEvidenceIndex(row.row_index)}>查看证据</button></td></tr>)}
        {!shown.length && <tr><td colSpan={7}>{loading ? '正在加载…' : error ? '读取失败，请重试。' : !summary ? '暂无业务记录，请创建回放快照。' : '没有匹配任务。'}</td></tr>}
      </tbody></table></div>
      <div className="sp-pagination"><button className="sp-button" disabled={!tasks || loading || page <= 1} onClick={() => setPage(value => value - 1)}>上一页</button><span>第 {page} / {Math.max(1, Math.ceil((tasks?.total_rows || 0) / 20))} 页</span><button className="sp-button" disabled={!tasks || loading || page * 20 >= tasks.total_rows} onClick={() => setPage(value => value + 1)}>下一页</button></div>
    </section><aside className="lg-right-stack"><section className="sp-section"><h2>风险分布与辅助指标</h2><p className="sp-muted">当前快照范围，分布不代表人员责任或风险成因。</p><div className="lg-actions"><button className="sp-button" disabled={!summary} onClick={() => setTopic('aois')}>AOI 风险分布</button><button className="sp-button" disabled={!summary} onClick={() => setTopic('couriers')}>人员负荷与风险分布</button><button className="sp-button" disabled={!summary} onClick={() => setTopic('quality')}>字段质量</button><button className="sp-button" disabled={!summary} onClick={() => setTopic('recent_accepted')}>近期接单</button><button className="sp-button" disabled={!summary} onClick={() => setTopic('recent_completed')}>近期完成</button><button className="sp-button" onClick={() => setView('catalog')}>指标目录与口径</button></div></section></aside></div>
    <section className="lg-topics"><div className="lg-section-heading"><h2>业务专题</h2><span className="sp-muted">当前快照范围</span></div><div className="lg-topic-grid">
      <button className="sp-section" disabled={!summary} onClick={() => { select('risk'); document.getElementById('logistics-task-list')?.scrollIntoView({ behavior: 'smooth', block: 'start' }); }}><h3>风险与积压</h3><p>查看逾期、临期和窗口缺失任务。</p></button>
      <div className="sp-section"><h3>揽收时效</h3><p>最近 {summary?.recent_minutes ?? '—'} 分钟：接单 {summary ? summary.metrics.recent_accepted.value ?? '不可计算' : '—'} 条，完成 {summary ? summary.metrics.recent_completed.value ?? '不可计算' : '—'} 条。</p><div className="lg-actions"><button className="sp-button" disabled={!summary} onClick={() => setTopic('recent_accepted')}>查看接单明细</button><button className="sp-button" disabled={!summary} onClick={() => setTopic('recent_completed')}>查看完成明细</button></div></div>
      <button className="sp-section" disabled={!summary} onClick={() => setTopic('couriers')}><h3>人员负荷</h3><p>按快递员查看样本任务与风险分布。</p><span className="lg-topic-entry">进入专题</span></button>
      <button className="sp-section" disabled={!summary} onClick={() => setTopic('quality')}><h3>字段质量</h3><p>分别查看窗口、位置及可见 GPS 字段。</p><span className="lg-topic-entry">进入专题</span></button>
    </div></section>
    </>}
    <MetricDetails definition={details} onClose={() => setDetails(null)} />
    <TaskEvidence rowIndex={evidenceIndex} onClose={() => setEvidenceIndex(null)} />
    <TopicPanel topic={topic} onClose={() => setTopic(null)} />
  </div>;
}
