import { useEffect, useRef, useState } from 'react';
import { Clock3, SlidersHorizontal, X } from 'lucide-react';
import { useSnapshot } from './SnapshotContext';

export default function ScopeBar({ onConfigure }: { onConfigure: () => void }) {
  const { config, region, at, snapshot, summary, status, error, create, retrySummary, reloadConfig } = useSnapshot();
  const dialog = useRef<HTMLDialogElement>(null);
  const [draftRegion, setDraftRegion] = useState(''), [draftAt, setDraftAt] = useState('');
  const busy = status === 'creating' || status === 'loading';
  useEffect(() => { if (status === 'ready') dialog.current?.close(); }, [status]);
  useEffect(() => { if (dialog.current?.open) setDraftRegion(region); }, [region]);

  function open() {
    setDraftRegion(region); setDraftAt(at); dialog.current?.showModal();
  }

  return <section className="lg-scope-toolbar" aria-label="回放范围">
    <div className="lg-scope-current" role="status">
      {snapshot && status === 'ready' ? <><span>上海 · 区域 {snapshot.region_ids.join('、')}</span><span aria-hidden="true">·</span><span>参考时点 {snapshot.business_as_of}</span></> : <span>尚未应用数据范围</span>}
      {busy && <span>正在更新指标…</span>}
      {status === 'dirty' && <span>范围已修改，待应用</span>}
      {status === 'error' && <span>读取失败，请调整范围后重试</span>}
    </div>
    <div className="lg-actions"><button className="sp-button" disabled={busy} onClick={open}><Clock3 size={16} aria-hidden="true" />调整范围</button><button className="sp-button" onClick={onConfigure}><SlidersHorizontal size={16} aria-hidden="true" />配置指标</button></div>
    <dialog ref={dialog} className="lg-scope-drawer" aria-labelledby="scope-drawer-title">
      <div className="lg-scope-drawer-heading"><h2 id="scope-drawer-title">调整数据范围</h2><button className="sp-button sp-icon-button" aria-label="关闭范围" onClick={() => dialog.current?.close()}><X size={18} /></button></div>
      <form className="lg-scope-form" onSubmit={event => { event.preventDefault(); void create({ region: draftRegion, at: draftAt }); }}>
        <label>授权区域<select aria-label="回放区域" value={draftRegion} disabled={busy || !config} onChange={event => setDraftRegion(event.target.value)}>
          {!config ? <option value="">等待加载授权范围</option> : <><option value="all">全部授权区域</option>{config.region_ids.map(value => <option key={value} value={value}>区域 {value}</option>)}</>}
        </select></label>
        <label>参考时点<input aria-label="回放参考时点" type="datetime-local" step="1" min="2000-01-01T00:00:00" max="2000-12-31T23:59:59" required value={draftAt} disabled={busy} onChange={event => setDraftAt(event.target.value)} /></label>
        <p className="sp-muted">历史回放 · 2000 年仅为参考时间轴，真实年份未知。</p>
        {config && !config.configured && <p role="status">尚未保存数据批次，请联系数据管理员。</p>}
        {busy && <p role="status">正在更新指标…</p>}
        {error && <div role="alert"><p>{error}</p>{snapshot && <button type="button" className="sp-button" disabled={busy} onClick={() => void retrySummary()}>重试读取指标</button>}</div>}
        <details className="lg-scope-details"><summary>范围说明</summary>
          <section aria-label="演示流程"><h3>揽收风险调查 Demo</h3><p>创建快照 → 调查与证据 → 人工反馈 → 跨账号审核 → 内部回执 → 新快照复核。</p><p>公开历史数据回放；模拟回执和历史完成均不能证明真实处置效果。批准不等于执行。</p></section>
          <p>窗口与静态属性按接单时已知回放；样本内数量不能推断整个区域没有风险。</p>
          {summary?.limitations.map(text => <p key={text}>{text}</p>)}
          <button type="button" className="sp-button" disabled={busy} onClick={reloadConfig}>重新加载数据范围</button>
        </details>
        <div className="lg-scope-drawer-actions"><button type="button" className="sp-button" onClick={() => dialog.current?.close()}>取消</button><button className="sp-button sp-primary" disabled={busy || !config?.configured || !draftRegion}>{busy ? '正在更新…' : '应用范围'}</button></div>
      </form>
    </dialog>
  </section>;
}
