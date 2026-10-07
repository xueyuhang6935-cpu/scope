import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { ArrowRight, Database } from 'lucide-react';
import { Button, DataTable, StatusTag } from './components';
import { dataRequest, jsonRequest } from './dataApi';
import { useBusinessAccess } from './BusinessAccess';

type Connection = {
  configured: boolean; connected: boolean; host: string; port: string; database: string; schema: string;
  scenarioId: string; workspaceId?: string; message?: string; tables?: string[]; scenarios?: Array<{ id: string; name: string }>;
  sourceType?: 'lade' | 'supply_demo'; defaultTable?: string;
};
type Preview = { columns: string[]; rows: Record<string, unknown>[]; total: number; pageSize: number; factoryScoped: boolean };

export default function DatabaseConnection({ factory }: { factory: string }) {
  const [connection, setConnection] = useState<Connection | null>(null);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  async function refresh() {
    setLoading(true); setError('');
    try { setConnection(await dataRequest<Connection>('/database')); }
    catch (cause) { setError((cause as Error).message); }
    finally { setLoading(false); }
  }
  useEffect(() => { void refresh(); }, []);
  return <><div className="sp-entry sp-connection-card"><Database size={22} /><strong>数据库连接</strong>
    <StatusTag tone={loading ? 'info' : error || connection?.message ? 'error' : connection?.connected ? 'success' : 'info'}>
      {loading ? '检查中' : error || connection?.message ? '连接异常' : connection?.connected ? '已连接' : '未配置'}
    </StatusTag>
    <p>{connection?.connected ? `${connection.database} · ${connection.schema} · ${connection.scenarios?.find(item => item.id === connection.scenarioId)?.name || connection.scenarioId}` : connection?.message || '连接业务数据库，查看可用数据。'}</p>
    <Button primary disabled={loading} onClick={() => setOpen(true)}>{connection?.configured ? '管理数据库' : '配置数据库'}<ArrowRight size={15} /></Button>
    {error && <p role="alert">{error}</p>}
  </div>{open && <ConnectionDialog connection={connection} factory={factory} onClose={() => setOpen(false)} onSaved={setConnection} />}</>;
}

function ConnectionDialog({ connection, factory, onClose, onSaved }: {
  connection: Connection | null; factory: string; onClose: () => void; onSaved: (value: Connection) => void;
}) {
  const { refresh: refreshAccess } = useBusinessAccess();
  const dialog = useRef<HTMLDialogElement>(null);
  const [current, setCurrent] = useState(connection);
  const [scenario, setScenario] = useState(connection?.scenarioId || 'normal');
  const [table, setTable] = useState(connection?.defaultTable || 'inventory_snapshots');
  const [region, setRegion] = useState('');
  const [orderId, setOrderId] = useState('');
  const [filters, setFilters] = useState({ region: '', order_id: '' });
  const [page, setPage] = useState(1);
  const [revision, setRevision] = useState(0);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [busy, setBusy] = useState(false);
  const [reading, setReading] = useState(false);
  const [error, setError] = useState(connection?.message || '');
  const [previewError, setPreviewError] = useState('');
  const [message, setMessage] = useState('');
  useLayoutEffect(() => {
    const previous = document.activeElement as HTMLElement;
    const overflow = document.body.style.overflow;
    const element = dialog.current;
    element?.showModal(); document.body.style.overflow = 'hidden';
    return () => { element?.close(); document.body.style.overflow = overflow; if (previous?.isConnected) previous.focus(); };
  }, []);
  const saved = current?.connected && scenario === current.scenarioId;
  const isLade = current?.sourceType === 'lade';
  useEffect(() => {
    setPreview(null); setPreviewError('');
    if (!saved) { setReading(false); return; }
    const controller = new AbortController();
    setReading(true);
    dataRequest<Preview>(`/database/preview?${new URLSearchParams({ table, page: String(page), factory: isLade ? 'all' : factory, ...(isLade && table === 'pickup_tasks' ? filters : {}) })}`, { signal: controller.signal })
      .then(setPreview).catch(cause => { if (!controller.signal.aborted) setPreviewError((cause as Error).message); })
      .finally(() => { if (!controller.signal.aborted) setReading(false); });
    return () => controller.abort();
  }, [saved, current?.scenarioId, table, page, factory, revision, isLade, filters]);
  async function perform(save: boolean) {
    if (busy) return;
    setBusy(true); setError(''); setMessage('');
    try {
      if (save) {
        const result = await jsonRequest<Connection>('/database', { scenarioId: scenario });
        if (result.workspaceId) { sessionStorage.setItem('business-workspace', result.workspaceId); refreshAccess(); }
        setCurrent(result); onSaved(result); setPage(1); setRevision(value => value + 1);
        if (!result.connected) setError(result.message || '连接验证失败，请重试。');
        else setMessage(result.sourceType === 'lade' ? '配置已保存，正在读取所选揽收批次。' : '配置已保存，正在读取所选场景。');
      } else {
        const result = await jsonRequest<{ message: string }>('/database/test', { scenarioId: scenario });
        setMessage(result.message);
        const status = await dataRequest<Connection>('/database');
        setCurrent(status); onSaved(status);
      }
    } catch (cause) { setError((cause as Error).message); }
    finally { setBusy(false); }
  }
  function cell(value: unknown) { return value == null ? '—' : typeof value === 'object' ? JSON.stringify(value) : String(value); }
  return <dialog ref={dialog} className="sp-dialog sp-import-dialog" aria-labelledby="database-dialog-title" onCancel={event => { event.preventDefault(); if (!busy) onClose(); }}>
    <div className="sp-section-heading"><h2 id="database-dialog-title">配置数据库</h2><Button disabled={busy} onClick={onClose}>关闭</Button></div>
    <p>{isLade ? '只读访问 LaDe-P 上海揽收任务。保留原始字段、来源行号和质量标记，可按任务编号回查。' : '使用服务器已配置的 PostgreSQL 连接，只读访问。当前数据为模拟数据。'}</p>
    {isLade && <p>原始时间未披露年份；GPS 缺失不会影响基础时效计算。</p>}
    <dl className="sp-facts"><div><dt>服务器</dt><dd>{current?.host || '未配置'}:{current?.port || '—'}</dd></div><div><dt>数据库 / Schema</dt><dd>{current?.database || '未配置'} / {current?.schema || 'supply_demo'}</dd></div></dl>
    <div className="sp-form-grid"><label>{isLade ? '导入批次' : '数据场景'}<select disabled={busy} value={scenario} onChange={event => { setScenario(event.target.value); setPage(1); setMessage(''); setError(''); }}>
      {!current?.scenarios?.length && <option value="normal">正常供应</option>}
      {current?.scenarios?.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
    </select></label></div>
    <div className="sp-actions"><Button disabled={busy} permission="connection" onClick={() => perform(false)}>测试连接</Button><Button primary disabled={busy} permission="connection" onClick={() => perform(true)}>{busy ? '处理中…' : '保存并接入'}</Button></div>
    {error && <p role="alert" className="sp-feedback sp-feedback-error">{error}</p>}
    {message && <p role="status">{message}</p>}
    {saved && <section aria-label="数据库数据预览" aria-busy={reading}>
      <h3>数据库数据预览</h3><p>{isLade ? '直接读取当前导入批次，分页展示完整原始记录和质量判定。' : '直接读取 PostgreSQL，按当前场景查询；包含场景完整时间线。'}</p>
      <div className="sp-form-grid"><label>数据表<select value={table} disabled={busy} onChange={event => { setTable(event.target.value); setPage(1); }}>
        {current?.tables?.map(name => <option key={name} value={name}>{name}</option>)}
      </select></label></div>
      {isLade && table === 'pickup_tasks' && <div className="sp-form-grid">
        <label>区域编号<input value={region} onChange={e => setRegion(e.target.value)} placeholder="全部区域" /></label>
        <label>任务编号<input value={orderId} onChange={e => setOrderId(e.target.value)} placeholder="输入原始 order_id" /></label>
        <Button disabled={reading || busy} onClick={() => { setFilters({ region: region.trim(), order_id: orderId.trim() }); setPage(1); }}>查询任务</Button>
      </div>}
      <div className="sp-actions"><Button disabled={reading || busy} onClick={() => setRevision(value => value + 1)}>刷新数据</Button></div>
      {reading && <p role="status">正在读取数据库…</p>}
      {previewError && <p role="alert" className="sp-feedback sp-feedback-error">{previewError}</p>}
      {preview && <><p>共 {preview.total} 条记录 · {isLade ? filters.region && table === 'pickup_tasks' ? `区域 ${filters.region}` : '当前导入批次' : preview.factoryScoped ? factory === 'all' ? '全部工厂' : `${factory} 工厂` : '场景公共数据'}{table === 'expected_results' && ' · 仅供测试核对，不作为调查证据'}</p>
        <DataTable caption="PostgreSQL 数据记录" headers={preview.columns}>{preview.rows.map((row, index) => <tr key={index}>{preview.columns.map(column => <td key={column}>{cell(row[column])}</td>)}</tr>)}</DataTable>
        {!preview.rows.length && <p>当前范围暂无记录。</p>}
        <div className="sp-pagination"><span>第 {page} / {Math.max(1, Math.ceil(preview.total / preview.pageSize))} 页</span>
          <Button disabled={reading || page === 1} onClick={() => setPage(value => value - 1)}>上一页</Button>
          <Button disabled={reading || page * preview.pageSize >= preview.total} onClick={() => setPage(value => value + 1)}>下一页</Button></div>
      </>}
    </section>}
  </dialog>;
}
