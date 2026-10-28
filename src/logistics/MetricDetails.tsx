import { useEffect, useRef } from 'react';
import { ArrowUpRight, Clock3, Database, PackagePlus, SquareCheck, TriangleAlert, X } from 'lucide-react';
import { useSnapshot } from './SnapshotContext';
import type { MetricDefinition } from './metricDefinitions';

export function MetricCard({ definition, selected, onClick, onDetails }: { definition: MetricDefinition; selected?: boolean; onClick: () => void; onDetails?: () => void }) {
  const { summary, status } = useSnapshot();
  const metric = summary?.metrics[definition.id];
  const value = status === 'creating' || status === 'loading' ? '加载中' : status === 'error' ? '读取失败' : !metric ? '—' : metric.status === 'empty' ? '空样本' : metric.value === null ? '不可计算' : metric.value.toLocaleString('zh-CN');
  const note = !metric ? status === 'dirty' ? '更新快照后查看' : status === 'error' ? '请重试读取' : '创建回放快照后查看' : metric.status === 'empty' ? '当前样本为空，不能推断区域无风险' : metric.value === null ? `已知至少 ${metric.known_lower_bound ?? 0} 条，存在窗口缺失` : '条 · 当前授权快照';
  const Icon = { backlog: Database, overdue: TriangleAlert, due_soon: Clock3, recent_accepted: PackagePlus, recent_completed: SquareCheck }[definition.id];
  const window = definition.id === 'backlog' ? '完整可见任务范围' : definition.id === 'overdue' ? '窗口终点早于参考时点' : definition.id === 'due_soon' ? `未来 ${summary?.horizon_minutes ?? '—'} 分钟窗口` : `最近 ${summary?.recent_minutes ?? '—'} 分钟`;
  const card = <button type="button" className={`lg-metric-card lg-${definition.tone}`} disabled={!metric || status !== 'ready'} aria-expanded={selected} onClick={onClick}>
    <span className="lg-metric-title">{definition.name}{onDetails ? <span className="lg-metric-icon"><Icon size={19} strokeWidth={1.7} aria-hidden="true" /></span> : <span className="lg-status">{metric?.status === 'empty' ? '空样本' : metric?.status === 'unavailable' ? '不可计算' : metric ? '已计算' : '待读取'}</span>}</span>
    {onDetails ? <span className="lg-metric-value"><strong>{value}</strong>{status === 'ready' && metric?.status === 'ok' && typeof metric.value === 'number' && <small>条</small>}</span> : <strong>{value}</strong>}
    {(!onDetails || status !== 'ready' || metric?.status !== 'ok') && <small>{note}</small>}
    {onDetails && <small className="lg-metric-description">{definition.description}</small>}
    {!onDetails && <span className="lg-metric-window">{definition.riskState ? definition.id === 'due_soon' ? `临期窗口：${summary?.horizon_minutes ?? '—'} 分钟` : '截至当前参考时点' : `最近 ${summary?.recent_minutes ?? '—'} 分钟`}</span>}
  </button>;
  return onDetails ? <div className={`lg-home-metric lg-${definition.tone}${selected ? ' is-selected' : ''}`}>{card}<div className="lg-home-metric-footer"><span>{window}</span><button type="button" className="lg-definition-button" aria-label={`查看${definition.name}口径`} onClick={onDetails}>口径<ArrowUpRight size={12} aria-hidden="true" /></button></div></div> : card;
}

export default function MetricDetails({ definition, onClose }: { definition: MetricDefinition | null; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const { snapshot, summary, status } = useSnapshot();
  const metric = definition && summary?.metrics[definition.id];
  const rule = definition && {
    backlog: '接单时点 ≤ T，且截至 T 未观察到完成。',
    overdue: '截至 T 未观察到完成，且窗口截止时点 < T。',
    due_soon: `截至 T 未观察到完成，且 T ≤ 窗口截止时点 ≤ T + ${summary?.horizon_minutes ?? '—'} 分钟（含边界）。`,
    recent_accepted: `接单时点位于 (T − ${summary?.recent_minutes ?? '—'} 分钟, T]，左边界不包含、右边界包含。`,
    recent_completed: `已观察到完成，完成时点位于 (T − ${summary?.recent_minutes ?? '—'} 分钟, T]，左边界不包含、右边界包含。`,
  }[definition.id];
  const dataState = status === 'error' ? '读取失败，请重试后查看。' : status === 'creating' || status === 'loading' ? '正在读取当前授权快照。' : !metric ? '尚无生效指标，请先创建回放快照。' : metric.status === 'empty' ? '当前样本为空，不能推断整个区域没有风险。' : metric.status === 'unavailable' ? `完整数量不可计算；已知至少 ${metric.known_lower_bound ?? 0} 条，存在窗口缺失。` : '数值由后端按当前授权快照计算，历史样本不代表生产全量。';
  useEffect(() => { if (definition) ref.current?.showModal(); else ref.current?.close(); }, [definition]);
  return <dialog ref={ref} className="lg-dialog lg-metric-dialog" aria-labelledby="metric-details-title" aria-describedby="metric-details-subtitle" onCancel={onClose} onClose={onClose}>
    <header className="lg-metric-dialog-header"><div><h2 id="metric-details-title">{definition?.name}</h2><p id="metric-details-subtitle">名称、范围与计算含义保持一致。</p></div><button type="button" className="lg-metric-dialog-close" aria-label="关闭口径说明" onClick={onClose} autoFocus><X size={18} aria-hidden="true" /></button></header>
    <dl className="lg-definition-list"><dt>业务含义</dt><dd>{definition?.description}</dd>
      <dt>统计范围</dt><dd>{snapshot ? `上海 · 区域 ${snapshot.region_ids.join('、')} · 同一回放快照` : '尚未生成快照'}</dd>
      <dt>参考时点</dt><dd>{snapshot ? `${snapshot.business_as_of}，真实年份未知` : '尚未生成快照'}</dd>
      <dt>口径说明</dt><dd>{rule}</dd>
      <dt>数据状态</dt><dd>{dataState}<small className="lg-metric-dialog-meta">单位：条（任务） · 口径版本：{summary?.metric_version || '生成快照后读取'}</small></dd>
    </dl>
  </dialog>;
}
