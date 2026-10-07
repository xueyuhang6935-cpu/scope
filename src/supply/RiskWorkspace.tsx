import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useLocation, useOutletContext, useParams, useSearchParams } from 'react-router-dom';
import { Button, DataTable, EmptyState, PageHeading, ScopedLink, StatusTag } from './components';
import type { SupplyContext } from './SupplyShell';
import { supplyRequest } from './supplyApi';
import InvestigationPanel from './InvestigationPanel';
import { useBusinessAccess } from './BusinessAccess';

export type ShortageItem = {
  factory: string; material: string; name: string; revision: string; unit: string; owner: string;
  computable: boolean; opening: string | null; shortage: string | null; firstShortageAt: string | null;
  totalDemand: string; missing: string[]; evidenceIds: string[];
  safetyWarnings: Array<{ at: string; balance: string; threshold: string }>;
  timeline: Array<{ at: string; kind: string; quantity: string; balance: string; planId?: string }>;
  affectedPlans: Array<{ planId: string; planVersion: number; bomVersion: number; at: string; quantity: string; uncovered: string; affectedVehicles: string }>;
};
export type Calculation = { scenarioId: string; scenarioName: string; asOf: string; windowStart: string; windowEnd: string; snapshotHash: string; results: ShortageItem[]; missing: string[]; affectedVehicles: string };
type Monitor = { configured: boolean; scenario_id: string; as_of: string; horizon_days: number; enabled: number; interval_seconds: number; last_attempt: string | null; error: string | null };
type Risk = { id: string; scenario_id: string; factory: string; material: string; revision: string; owner: string; status: string; version: number; recurrence_of: string | null; result: ShortageItem; updated_at: string };
type RiskDetail = Risk & { asOf: string; windowStart: string; windowEnd: string; snapshotHash: string; evidence: Array<{ id: string; sourceRecordId: string; knownAt: string; record: Record<string, unknown> }>; history: Array<{ id: number; version: number; kind: string; created_at: string; details: { status?: string; owner?: string } }> };
type RiskList = { events: Risk[]; latestScan: { id: string; created_at: string; result: Calculation } | null };
const labels: Record<string, string> = { open: '待处理', acknowledged: '已确认风险', data_required: '待补充数据', covered: '当前窗口已覆盖' };
export const showTime = (value?: string | null) => value ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '—';
const localTime = (value: string) => new Date(new Date(value).getTime() + 8 * 3600000).toISOString().slice(0, 16);

function useRiskData(factory: string, eventId?: string) {
  const [monitor, setMonitor] = useState<Monitor | null>(null);
  const [data, setData] = useState<RiskList | null>(null);
  const [detail, setDetail] = useState<RiskDetail | null>(null);
  const [error, setError] = useState('');
  const [version, setVersion] = useState(0);
  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    setData(null); setDetail(null); setError('');
    async function load() {
      try {
        const config = await supplyRequest<Monitor>('/monitor', undefined, controller.signal);
        if (controller.signal.aborted) return;
        setMonitor(config);
        if (config.configured) {
          if (eventId) {
            const item = await supplyRequest<RiskDetail>(`/risks/${eventId}?factory=${factory}`, undefined, controller.signal);
            if (!controller.signal.aborted) setDetail(item);
          } else {
            const list = await supplyRequest<RiskList>(`/risks?factory=${factory}`, undefined, controller.signal);
            if (!controller.signal.aborted) setData(list);
          }
        } else { setData(null); setDetail(null); }
        if (!controller.signal.aborted) setError('');
      } catch (cause) { if (!controller.signal.aborted) setError((cause as Error).message); }
      finally { if (!controller.signal.aborted) timer = setTimeout(load, 5000); }
    }
    void load(); return () => { controller.abort(); clearTimeout(timer); };
  }, [factory, eventId, version]);
  return { monitor, data, detail, error, refresh: () => setVersion(v => v + 1) };
}

function MonitorControls({ config, refresh }: { config: Monitor; refresh: () => void }) {
  const { allows } = useBusinessAccess();
  const [asOf, setAsOf] = useState(localTime(config.as_of));
  const [days, setDays] = useState(config.horizon_days);
  const [interval, setInterval] = useState(config.interval_seconds);
  const [enabled, setEnabled] = useState(!!config.enabled);
  const [purchase, setPurchase] = useState('PO-FOCUS');
  const [installment, setInstallment] = useState('01');
  const [arrival, setArrival] = useState('2026-10-08T08:00');
  const [requestId, setRequestId] = useState(crypto.randomUUID());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  async function act(path: string, body: unknown, message: string) {
    setBusy(true); setError(''); setNotice('');
    try { await supplyRequest(path, body); setNotice(message); if (path.endsWith('/commitment')) setRequestId(crypto.randomUUID()); refresh(); }
    catch (cause) { setError((cause as Error).message); }
    finally { setBusy(false); }
  }
  return <section className="sp-section" aria-label="风险监测控制">
    <div className="sp-section-heading"><h2>风险监测</h2><Button primary disabled={busy} permission="scan" onClick={() => void act('/monitor/scan', {}, '扫描完成，风险已更新。')}>{busy ? '处理中…' : '立即扫描'}</Button></div>
    <p><StatusTag tone={config.enabled ? 'success' : 'info'}>{config.enabled ? `自动扫描 · 每 ${config.interval_seconds} 秒` : '自动扫描已暂停'}</StatusTag> 场景：{config.scenario_id} · 最近检查：{showTime(config.last_attempt)}</p>
    <p>回放时点：{showTime(config.as_of)}（北京时间）。计算窗口从场景起点开始，使用当时已知的库存、需求和到货证据。</p>
    {config.error && <p role="alert">最近扫描失败：{config.error}。下方保留上次成功结果。</p>}
    {allows('monitor', ['A', 'B']) && <details><summary>监测设置与交期变更</summary>
      <div className="sp-form-grid">
        <label>回放时点（北京时间）<input aria-label="回放时点" type="datetime-local" value={asOf} onChange={e => setAsOf(e.target.value)} /></label>
        <label>计算天数<input aria-label="计算天数" type="number" min={1} max={14} value={days} onChange={e => setDays(Number(e.target.value))} /></label>
        <label>扫描间隔（秒）<input aria-label="扫描间隔" type="number" min={10} max={3600} value={interval} onChange={e => setInterval(Number(e.target.value))} /></label>
        <label className="sp-checkbox"><input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} />启用自动扫描</label>
      </div>
      <Button disabled={busy || !asOf} permission="monitor" onClick={() => void act('/monitor', { asOf: asOf + ':00+08:00', days, intervalSeconds: interval, enabled }, '设置已保存；启用后自动扫描。')}>保存监测设置</Button>
      <h3>注入模拟交期变更</h3><p>仅作用于当前账号的场景，原始模拟数据保留。提交后立即重新计算风险。</p>
      <div className="sp-form-grid">
        <label>采购行<input aria-label="采购行" value={purchase} onChange={e => { setPurchase(e.target.value); setRequestId(crypto.randomUUID()); }} /></label>
        <label>承诺批次<input aria-label="承诺批次" value={installment} onChange={e => { setInstallment(e.target.value); setRequestId(crypto.randomUUID()); }} /></label>
        <label>新承诺到线时间（北京时间）<input aria-label="新承诺到线时间" type="datetime-local" value={arrival} onChange={e => { setArrival(e.target.value); setRequestId(crypto.randomUUID()); }} /></label>
      </div>
      <Button disabled={busy || !arrival} onClick={() => void act('/monitor/commitment', { purchaseLineId: purchase, installment, availableAt: arrival + ':00+08:00', requestId }, '交期变更已记录，风险已重新扫描。')}>提交交期变更并扫描</Button>
    </details>}
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
  </section>;
}

export function CalculationSummary({ calculation }: { calculation: Calculation }) {
  return <><p>模拟场景：{calculation.scenarioName} · 已知时点：{showTime(calculation.asOf)}</p><p>计算窗口：{showTime(calculation.windowStart)} — {showTime(calculation.windowEnd)}</p>
    <DataTable caption="缺料计算结果" headers={['工厂 / 物料 / 版本', '可用期初 / 需求', '最大缺口', '首次缺料']}>
      {calculation.results.map(r => <tr key={`${r.factory}-${r.material}-${r.revision}`}><td>{r.factory} / {r.material}<small>{r.revision}</small></td><td>{r.computable ? Number(r.opening) : '不可计算'} / {Number(r.totalDemand)} {r.unit}</td><td>{r.computable ? `${Number(r.shortage)} ${r.unit}` : <span title={r.missing.join('；')}>数据不足</span>}</td><td>{showTime(r.firstShortageAt)}</td></tr>)}
    </DataTable></>;
}

export default function RiskWorkspace({ overview = false }: { overview?: boolean }) {
  const { factory } = useOutletContext<SupplyContext>();
  const location = useLocation();
  const [search, setSearch] = useSearchParams();
  const latestSearch = useRef(search);
  useLayoutEffect(() => { latestSearch.current = search; }, [search]);
  const { monitor, data, error, refresh } = useRiskData(factory);
  const query = search.get('q') || '', status = search.get('status') || '';
  const items = (data?.events || []).filter(e => (!status || e.status === status) && `${e.id} ${e.material} ${e.result.name}`.toLowerCase().includes(query.toLowerCase()));
  const pageCount = Math.max(1, Math.ceil(items.length / 10));
  const page = Math.min(pageCount, Math.max(1, Number(search.get('page')) || 1));
  const calculation = data?.latestScan?.result;
  const scopedResults = calculation?.results.filter(r => factory === 'all' || r.factory === factory) || [];
  function filter(key: string, value: string) { const next = new URLSearchParams(latestSearch.current); next.delete('page'); value ? next.set(key, value) : next.delete(key); latestSearch.current = next; setSearch(next, { replace: true }); }
  return <><PageHeading title={overview ? '工作台' : '风险处置'} subtitle="模拟数据库 · 确定性缺料计算与持续监测" action={overview ? <ScopedLink to="/assistant" className="sp-button">打开智能助手</ScopedLink> : undefined} />
    {error && <p role="alert">{error}<Button onClick={refresh}>重试</Button></p>}
    {!monitor ? <p>正在加载监测配置…</p> : !monitor.configured ? <EmptyState title="尚未接入计算数据"><p>在数据接入中选择 PostgreSQL 场景并保存，即可启用缺料计算和风险监测。</p><ScopedLink to="/data/connections">配置数据库</ScopedLink></EmptyState> : <>
      <MonitorControls key={monitor.scenario_id} config={monitor} refresh={refresh} />
      <div className="sp-metrics">
        {[['持续缺料', data?.events.filter(e => ['open', 'acknowledged'].includes(e.status)).length || 0], ['待补充数据', data?.events.filter(e => e.status === 'data_required').length || 0], ['已覆盖待核验', data?.events.filter(e => e.status === 'covered').length || 0], ['安全库存告警', scopedResults.filter(r => r.safetyWarnings.length).length]].map(([label, n]) => <div className="sp-metric" key={label}><span>{label}</span><strong>{n}</strong><small>当前工厂范围</small></div>)}
      </div>
      {calculation && <p>最近计算：{showTime(data?.latestScan?.created_at)} · {calculation.scenarioName} · 知识时点 {showTime(calculation.asOf)}。已覆盖不等于已完成收货核验或正式关闭。</p>}
      {!!calculation?.missing.length && <div role="alert">数据不完整：{calculation.missing.join('；')}</div>}
      <div className="sp-filters"><label>搜索风险<input aria-label="搜索风险" value={query} onChange={e => filter('q', e.target.value)} placeholder="物料、名称或事件编号" /></label><label>状态<select aria-label="风险状态" value={status} onChange={e => filter('status', e.target.value)}><option value="">全部</option>{Object.entries(labels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label><Button onClick={refresh}>刷新</Button></div>
      {items.length ? <><DataTable caption="风险事件列表" headers={['物料 / 事件', '首次缺料 / 缺口', '状态', '负责人', '操作']}>
        {items.slice((page - 1) * 10, page * 10).map(e => <tr key={e.id}><td><strong>{e.material} · {e.result.name}</strong><small>{e.factory} 工厂 · {e.revision}</small><small>{e.id}</small></td><td>{showTime(e.result.firstShortageAt)}<small>{e.result.computable ? `${Number(e.result.shortage)} ${e.result.unit}` : '数据不足，不推算缺口'}</small></td><td><StatusTag tone={e.status === 'covered' ? 'info' : e.status === 'data_required' ? 'warning' : 'error'}>{labels[e.status]}</StatusTag></td><td>{e.owner}</td><td><ScopedLink to={`/risks/${e.id}?from=${encodeURIComponent(location.pathname + location.search)}`}>查看事件</ScopedLink></td></tr>)}
      </DataTable><div className="sp-pagination"><span>共 {items.length} 条 · 第 {page} / {pageCount} 页</span><Button disabled={page === 1} onClick={() => { const next = new URLSearchParams(search); next.set('page', String(page - 1)); setSearch(next); }}>上一页</Button><Button disabled={page === pageCount} onClick={() => { const next = new URLSearchParams(search); next.set('page', String(page + 1)); setSearch(next); }}>下一页</Button></div></> : <EmptyState title={data?.latestScan ? '当前筛选范围内没有风险事件' : '等待首次扫描'}><p>{data?.latestScan ? '查看下方计算结果与数据完整性；空列表不能替代数据核验。' : '点击立即扫描，或等待自动扫描。'}</p></EmptyState>}
      {calculation && <details><summary>查看全部物料计算结果</summary><CalculationSummary calculation={{ ...calculation, results: scopedResults }} /></details>}
    </>}
  </>;
}

function LogisticsUpdate({ event, refresh }: { event: RiskDetail; refresh: () => void }) {
  const [purchase, setPurchase] = useState('PO-FOCUS'), [arrival, setArrival] = useState('2026-10-08T08:00'), [note, setNote] = useState(''), [message, setMessage] = useState(''), [busy, setBusy] = useState(false);
  async function submit() { setBusy(true); try { await supplyRequest('/monitor/commitment', { requestId: crypto.randomUUID(), purchaseLineId: purchase, availableAt: arrival + ':00+08:00', note }); setMessage('交期已更新，风险已重新扫描。'); refresh(); } catch (e) { setMessage((e as Error).message); } finally { setBusy(false); } }
  return <section className="sp-section"><h2>采购／物流业务更新</h2><p>仅能更新当前账号负责事件所对应的采购交期：{event.material} · {event.factory} 厂。</p><div className="sp-form-grid"><label>负责采购行<input value={purchase} onChange={e => setPurchase(e.target.value)} /></label><label>承诺到线时间<input type="datetime-local" value={arrival} onChange={e => setArrival(e.target.value)} /></label><label>业务说明<input value={note} maxLength={200} onChange={e => setNote(e.target.value)} /></label></div><Button disabled={busy || !note.trim()} onClick={() => void submit()}>更新负责事件交期</Button>{message && <p role="status">{message}</p>}</section>;
}

function BalanceChart({ item }: { item: ShortageItem }) {
  if (!item.computable || !item.timeline.length) return null;
  const values = [Number(item.opening), ...item.timeline.map(t => Number(t.balance))];
  const min = Math.min(0, ...values), max = Math.max(1, ...values), range = max - min;
  const y = (n: number) => 160 - (n - min) / range * 130;
  const points = values.map((n, i) => `${60 + i / Math.max(1, values.length - 1) * 600},${y(n)}`).join(' ');
  return <svg viewBox="0 0 720 190" role="img" aria-label="预计库存结余曲线" style={{ width: '100%', maxHeight: 240 }}><title>按供需发生顺序展示结余；零线以下为缺料</title><line x1="60" x2="660" y1={y(0)} y2={y(0)} stroke="#888" strokeDasharray="4" /><polyline points={points} fill="none" stroke="#7853ad" strokeWidth="2" /><text x="4" y="32" fontSize="12">{max}</text><text x="4" y="160" fontSize="12">{min}</text><text x="60" y="185" fontSize="12">期初 → 各次供需变化（{item.unit}）</text></svg>;
}

export function CalculatedRiskDetail() {
  const { info, allows } = useBusinessAccess();
  const { factory } = useOutletContext<SupplyContext>();
  const { eventId } = useParams();
  const [search] = useSearchParams();
  const { detail: e, error, refresh } = useRiskData(factory, eventId);
  const [owner, setOwner] = useState('');
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState('');
  const from = search.get('from');
  const back = from && ['/risks', '/workspace'].includes(from.split('?')[0]) ? from : '/risks';
  async function update(status?: string) {
    if (!e) return;
    setBusy(true); setActionError('');
    try { await supplyRequest(`/risks/${e.id}`, { version: e.version, owner: owner.trim() || e.owner, status: status || e.status }); setOwner(''); refresh(); }
    catch (cause) { setActionError((cause as Error).message); } finally { setBusy(false); }
  }
  return <><ScopedLink to={back}>返回列表</ScopedLink>{error && <p role="alert">{error}<Button onClick={refresh}>重试</Button></p>}{!e ? !error && <p>正在读取风险事件…</p> : <>
    <PageHeading title={`${e.material} · ${e.result.name}`} subtitle={`${e.factory} 工厂 · ${e.scenario_id} · 事件版本 ${e.version}`} action={<StatusTag tone={e.status === 'covered' ? 'info' : 'warning'}>{labels[e.status]}</StatusTag>} />
    <section className="sp-section"><h2>缺料影响</h2><p>{e.id} · {e.revision}</p><dl className="sp-facts"><div><dt>首次缺料</dt><dd>{showTime(e.result.firstShortageAt)}</dd></div><div><dt>最大缺口</dt><dd>{e.result.computable ? `${Number(e.result.shortage)} ${e.result.unit}` : '不可计算'}</dd></div><div><dt>可用期初</dt><dd>{e.result.computable ? Number(e.result.opening) : '不可计算'}</dd></div><div><dt>已知证据截止</dt><dd>{showTime(e.asOf)}</dd></div></dl>
      <p>计算窗口：{showTime(e.windowStart)} — {showTime(e.windowEnd)}。数据快照：<span style={{ overflowWrap: 'anywhere' }}>{e.snapshotHash}</span></p>
      {e.result.missing.length > 0 && <ul>{e.result.missing.map(m => <li key={m}>{m}</li>)}</ul>}<BalanceChart item={e.result} />
      {e.recurrence_of && <p>复发关联：<ScopedLink to={`/risks/${e.recurrence_of}`}>{e.recurrence_of}</ScopedLink></p>}
      <p>负责人：{e.owner}</p><label>新负责人<input aria-label="新负责人" value={owner} maxLength={100} onChange={ev => setOwner(ev.target.value)} /></label><div className="sp-actions"><Button disabled={busy || !owner.trim()} permission="risk.update" onClick={() => void update()}>分配负责人</Button>{e.status === 'open' && <Button primary disabled={busy} permission="risk.update" onClick={() => void update('acknowledged')}>确认风险</Button>}{e.status === 'acknowledged' && <Button disabled={busy} permission="risk.update" onClick={() => void update('open')}>恢复待处理</Button>}</div>{actionError && <p role="alert">{actionError}</p>}
      <p>“当前窗口已覆盖”仅表示计算缺口消失，不代表完成收货核验、审批执行或正式关闭。</p>
    </section>
    {allows('event.update', [e.factory]) && info?.account === e.owner && <LogisticsUpdate event={e} refresh={refresh} />}
    <InvestigationPanel key={e.id} eventId={e.id} />
    <section className="sp-section"><h2>受影响生产计划</h2><DataTable caption="受影响生产计划" headers={['计划 / 版本', '需求时间', '需求 / 未覆盖', '受影响车辆']}>
      {e.result.affectedPlans.map((p, i) => <tr key={`${p.planId}-${i}`}><td>{p.planId}<small>计划 v{p.planVersion} · BOM v{p.bomVersion}</small></td><td>{showTime(p.at)}</td><td>{Number(p.quantity)} / {Number(p.uncovered)}</td><td>{Number(p.affectedVehicles)}</td></tr>)}
    </DataTable></section>
    <section className="sp-section"><h2>供需时间线</h2><DataTable caption="供需时间线" headers={['时间', '类型', '数量', '预计结余']}>
      {e.result.timeline.map((t, i) => <tr key={i}><td>{showTime(t.at)}</td><td>{t.kind === 'supply' ? '供给 / 库存变动' : '生产需求'}<small>{t.planId}</small></td><td>{Number(t.quantity)}</td><td>{Number(t.balance)}</td></tr>)}
    </DataTable></section>
    <section className="sp-section"><h2>来源证据</h2>{e.evidence.map(ev => <details key={ev.id}><summary>{ev.id} · 获知于 {showTime(ev.knownAt)}</summary><p>{ev.sourceRecordId}</p><pre style={{ overflowX: 'auto', maxWidth: '100%' }}>{JSON.stringify(ev.record, null, 2)}</pre></details>)}</section>
    <section className="sp-section"><h2>事件历史</h2>{e.history.map(h => <p key={h.id}>{showTime(h.created_at)} · v{h.version} · {({ detected: '发现风险', scheduled: '自动扫描', manual: '手动扫描', commitment_changed: '交期变更', manual_update: '人工更新' } as Record<string, string>)[h.kind] || h.kind} · {labels[h.details.status || ''] || ''}{h.details.owner ? ` · ${h.details.owner}` : ''}</p>)}</section>
  </>}</>;
}
