import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { FileText } from 'lucide-react';
import { PageHeading } from '../supply/components';
import { useAccess } from './LogisticsShell';
import { request, type KnowledgeEntry } from './api';
import { beijingTime } from './ReportView';

export default function Knowledge() {
  const { access } = useAccess();
  const [search, setSearch] = useState(''), [region, setRegion] = useState(''), [status, setStatus] = useState(''), [sort, setSort] = useState('archived');
  const [from, setFrom] = useState(''), [to, setTo] = useState(''), [page, setPage] = useState(1), [revision, setRevision] = useState(0);
  const [value, setValue] = useState<{ rows: KnowledgeEntry[]; total_rows: number } | null>(null), [error, setError] = useState('');
  const regions = [...new Set(access?.roles.flatMap(role => role.regions) || [])];
  useEffect(() => {
    const controller = new AbortController(); setValue(null); setError('');
    request<typeof value>(`/knowledge?${new URLSearchParams({ search, region, status, sort, page: String(page), reference_from: from, reference_to: to })}`, undefined, controller.signal)
      .then(result => { if (!controller.signal.aborted) setValue(result); })
      .catch(cause => { if (!controller.signal.aborted) setError(cause.message); });
    return () => controller.abort();
  }, [search, region, status, sort, from, to, page, revision]);
  function filter(setter: (value: string) => void, value: string) { setter(value); setPage(1); }
  return <div className="lg-knowledge"><PageHeading title="知识库" subtitle="查找已有调查成果，核对原始依据，并在新对话中引用" />
    <section className="sp-section lg-knowledge-hero"><div><h2>可复用的调查成果</h2><p>保留原参考时点、来源快照、任务证据与结果限制。</p></div><strong>{value?.total_rows ?? '—'}<small>份授权成果</small></strong></section>
    <section className="sp-section lg-knowledge-tools"><label>搜索成果<input aria-label="搜索知识库" value={search} maxLength={200} placeholder="报告名称、调查问题或任务编号" onChange={event => filter(setSearch, event.target.value)} /></label>
      <label>区域<select value={region} onChange={event => filter(setRegion, event.target.value)}><option value="">全部授权区域</option>{regions.map(id => <option key={id} value={id}>区域 {id}</option>)}</select></label>
      <label>成果状态<select value={status} onChange={event => filter(setStatus, event.target.value)}><option value="">全部成果</option><option value="complete">完整报告</option><option value="partial">部分结果</option></select></label>
      <label>排序<select value={sort} onChange={event => filter(setSort, event.target.value)}><option value="archived">最近归档</option><option value="reference">参考时点</option></select></label>
      <label>参考时点起<input type="datetime-local" min="2000-01-01T00:00" max="2000-12-31T23:59" value={from} onChange={event => filter(setFrom, event.target.value)} /></label>
      <label>参考时点止<input type="datetime-local" min="2000-01-01T00:00" max="2000-12-31T23:59" value={to} onChange={event => filter(setTo, event.target.value)} /></label>
    </section>
    {error ? <div role="alert"><p>{error}</p><button className="sp-button" onClick={() => setRevision(value => value+1)}>重试读取成果</button></div> : !value ? <p role="status">正在读取授权成果…</p> : <>
      <div className="lg-knowledge-grid">{value.rows.map(item => <article className="sp-section lg-knowledge-card" key={item.knowledge_id}><div className="lg-section-heading"><FileText size={24} /><span className={`lg-status lg-${item.result_status === 'partial' ? 'orange' : 'green'}`}>{item.result_status === 'partial' ? '部分结果' : '完整报告'}</span></div>
        <h2>{item.title}</h2><p className="sp-muted">区域 {item.scope.region_ids.join('、')} · {item.scope.business_as_of}</p><p>{item.abstract}</p><p className="sp-muted">{item.result_kind === 'model' ? '实验模型调查成果' : '确定性快照报告'} · 归档 {beijingTime(item.archived_at)}（北京时间）</p>
        <div className="sp-actions"><Link className="sp-button" to={`/knowledge/${item.knowledge_id}`}>阅读成果</Link><Link to={`/knowledge/${item.knowledge_id}?cite=1`}>引用到新对话</Link></div>
      </article>)}</div>
      {!value.rows.length && <section className="sp-empty"><h2>暂无匹配成果</h2><p>已保存的完整或部分调查成果按当前授权范围展示。</p></section>}
      <div className="sp-pagination"><span>{value.total_rows} 份成果 · 第 {page} 页</span><button className="sp-button" disabled={page <= 1} onClick={() => setPage(value => value-1)}>上一页</button><button className="sp-button" disabled={page*20 >= value.total_rows} onClick={() => setPage(value => value+1)}>下一页</button></div>
    </>}
    <p className="sp-muted">阅读与引用不会重新调查；历史结论以原参考时点为准。2000 年仅为参考时间轴，真实年份未知。</p>
  </div>;
}
