import { useEffect, useRef, useState } from 'react';
import { request } from './api';
import { useSnapshot } from './SnapshotContext';
import { minutes, riskNames } from './metricDefinitions';

export type EvidenceTask = { order_id: string; region_id: string; courier_id: string; aoi_id: string; accept_time_reference: string;
  time_window_end_reference: string | null; current_wait_minutes?: number; current_overdue_minutes?: number | null;
  risk_state?: string; completion_observed: boolean; pickup_time_reference?: string; source_file: string; source_row: number;
  visible_quality: Record<string, string>; lng?: number; lat?: number; accept_gps_time_reference?: string; pickup_gps_time_reference?: string };
type Evidence = { record: EvidenceTask; business_as_of: string; snapshot_id: string; limitations: string[] };
export default function TaskEvidence({ rowIndex, onClose }: { rowIndex: number | null; onClose: () => void }) {
  const { snapshot, invalidate } = useSnapshot();
  const dialog = useRef<HTMLDialogElement>(null);
  const [value, setValue] = useState<Evidence | null>(null), [error, setError] = useState(''), [revision, setRevision] = useState(0);
  useEffect(() => {
    setValue(null); setError('');
    if (rowIndex === null || !snapshot) { dialog.current?.close(); return; }
    dialog.current?.showModal();
    const controller = new AbortController();
    request<Evidence>(`/snapshots/${encodeURIComponent(snapshot.snapshot_id)}/task-evidence/${rowIndex}`, undefined, controller.signal)
      .then(data => { if (!controller.signal.aborted) setValue(data); })
      .catch(cause => { if (!controller.signal.aborted) { if ([401, 403, 409].includes(cause.status)) invalidate(cause.message); else setError(cause.message); } });
    return () => controller.abort();
  }, [snapshot, rowIndex, revision]);
  const record = value?.record;
  const qualityNames: Record<string, string> = { window: '窗口', stop_coordinates: '任务位置', accept_gps: '接单 GPS', pickup_gps: '完成 GPS' };
  return <dialog ref={dialog} className="lg-dialog" aria-labelledby="task-evidence-title" onCancel={onClose} onClose={onClose}>
    <h2 id="task-evidence-title">任务快照证据</h2>
    {error ? <div role="alert"><p>{error}</p><button className="sp-button" onClick={() => setRevision(value => value + 1)}>重试读取证据</button></div> : !record ? <p role="status">正在读取证据…</p> : <>
      <p>任务 {record.order_id} · 截至 {value.business_as_of}</p>
      <dl className="lg-definition-list"><dt>区域</dt><dd>{record.region_id}</dd><dt>快递员／AOI</dt><dd>{record.courier_id} / {record.aoi_id}</dd>
        <dt>当前状态</dt><dd>{record.completion_observed ? '已观察完成' : riskNames[record.risk_state || '']}</dd><dt>接单时点</dt><dd>{record.accept_time_reference}</dd><dt>窗口截止</dt><dd>{record.time_window_end_reference || '不可计算'}</dd>
        {record.completion_observed ? <><dt>可见完成时点</dt><dd>{record.pickup_time_reference}</dd></> : <><dt>当前等待</dt><dd>{minutes(record.current_wait_minutes ?? null)}</dd><dt>当前逾期</dt><dd>{minutes(record.current_overdue_minutes ?? null)}</dd></>}
        <dt>可见位置</dt><dd>{record.lng == null ? '无可用位置' : `${record.lng}, ${record.lat}`}</dd><dt>来源</dt><dd>{record.source_file} · 行 {record.source_row}</dd>
        {Object.entries(record.visible_quality).map(([field, state]) => <span className="lg-definition-pair" key={field}><dt>{qualityNames[field] || field}</dt><dd>{field === 'pickup_gps' && !record.completion_observed ? '未发生完成，不适用' : state === 'available' ? '可用' : state === 'unavailable' ? '不可用' : state}</dd></span>)}
      </dl><p className="sp-muted">仅展示该任务在当前授权快照中可见的字段和程序计算，不包含未来完成信息。{value.limitations.join(' ')}</p>
    </>}
    <button className="sp-button sp-primary" onClick={onClose}>关闭证据</button>
  </dialog>;
}
