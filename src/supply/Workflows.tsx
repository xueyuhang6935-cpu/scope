import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import { useOutletContext } from 'react-router-dom';
import { ArrowLeft, ArrowRight, Check, FileInput, RotateCcw, Save, X } from 'lucide-react';
import { Button, DataTable, PageHeading, ScopedLink, StatusTag } from './components';
import type { SupplyContext } from './SupplyShell';
import { RealImportWorkflow } from './RealData';

type Repair = { owner: string; note: string; factor: string; evidence: string; confirmed: boolean; version: number; checked: boolean };
type Simulation = { repair: Repair; imports: number; approval?: { decision: string; comment: string } };
const initial: Simulation = { repair: { owner: '', note: '', factor: '', evidence: '', confirmed: false, version: 1, checked: false }, imports: 0 };
const SimulationContext = createContext<{ state: Simulation; commit: (next: Simulation) => void }>({ state: initial, commit: () => {} });

export function SimulationProvider({ user, children }: { user: string; children: ReactNode }) {
  const key = `supply-simulation-v1:${user}`;
  const [state, setState] = useState<Simulation>(() => {
    try {
      const saved = JSON.parse(sessionStorage.getItem(key) || 'null');
      if (saved && typeof saved.imports === 'number' && saved.repair && typeof saved.repair.version === 'number' && typeof saved.repair.factor === 'string') return saved;
    } catch {}
    return initial;
  });
  function commit(next: Simulation) {
    try { sessionStorage.setItem(key, JSON.stringify(next)); }
    catch { throw new Error('无法保存本次模拟记录，请检查浏览器存储空间后重试。'); }
    setState(next);
  }
  return <SimulationContext.Provider value={{ state, commit }}>{children}</SimulationContext.Provider>;
}
export function useSimulation() { return useContext(SimulationContext); }

function Confirmation({ title, children, onCancel, onConfirm }: { title: string; children: ReactNode; onCancel: () => void; onConfirm: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement;
    const dialog = ref.current;
    dialog?.showModal();
    return () => { dialog?.close(); if (previous?.isConnected && previous !== document.body) previous.focus(); };
  }, []);
  return <dialog ref={ref} className="sp-dialog" aria-labelledby="confirmation-title" onCancel={event => { event.preventDefault(); onCancel(); }}><div className="sp-section-heading"><h2 id="confirmation-title">{title}</h2><Button aria-label="关闭确认弹窗" title="关闭" className="sp-icon-button" onClick={onCancel}><X size={18} /></Button></div>{children}<div className="sp-actions"><Button onClick={onCancel}>取消</Button><Button primary onClick={onConfirm}><Check size={16} />确认模拟提交</Button></div></dialog>;
}
function Feedback({ error, message }: { error?: string; message?: string }) {
  return <>{error && <p role="alert" className="sp-feedback sp-feedback-error">{error}</p>}{message && <p role="status" className="sp-feedback">{message}</p>}</>;
}
function Steps({ titles, current }: { titles: string[]; current: number }) {
  return <ol className="sp-steps">{titles.map((title, index) => <li key={title} aria-current={index === current ? 'step' : undefined}><span>{index < current ? <Check size={14} /> : index + 1}</span>{title}</li>)}</ol>;
}
function ResponseChoice({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  return <label>模拟响应<select value={value} onChange={event => onChange(event.target.value)}><option value="ok">返回处理结果</option><option value="error">返回服务错误</option></select></label>;
}

export function ImportWorkflow() {
  const { factory } = useOutletContext<SupplyContext>();
  const { state, commit } = useSimulation();
  const [step, setStep] = useState(0);
  const [mode, setMode] = useState('file');
  const [file, setFile] = useState<File | null>(null);
  const [mapping, setMapping] = useState({ material: '物料编码', quantity: '数量', unit: '单位' });
  const [ack, setAck] = useState(false);
  const [response, setResponse] = useState('ok');
  const [error, setError] = useState('');
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [batch, setBatch] = useState(0);
  const repaired = state.repair.checked;
  const blocked = factory !== 'A' && !repaired;
  const rows = factory === 'A' ? ['P001'] : factory === 'B' ? ['P012'] : ['P001', 'P012'];
  if (mode === 'file') return <RealImportWorkflow factory={factory} onDemo={() => setMode('demo')} />;
  function next() {
    setError('');
    if (step === 0 && mode === 'file') {
      if (!file) return setError('请选择文件。');
      if (!/\.(csv|xlsx)$/i.test(file.name) || file.size === 0 || file.size > 10 * 1024 * 1024) return setError('请选择非空 CSV 或 XLSX 文件，大小不超过 10 MB。');
      return setError('文件解析与导入服务尚未接入，文件未上传、未解析。可切换到模拟样例继续。');
    }
    if (step === 1 && (Object.values(mapping).some(value => !value) || new Set(Object.values(mapping)).size !== 3)) return setError('每个必填字段须选择不同的来源列。');
    if (step === 1 && (mapping.material !== '物料编码' || mapping.quantity !== '数量' || mapping.unit !== '单位')) return setError('来源列类型不匹配：物料编码应映射物料编码，库存数量应映射数量，库存单位应映射单位。');
    if (step === 2 && blocked && !ack) return setError('请确认隔离不可计算的 P012 记录后继续，或先完成单位换算复检。');
    setStep(step + 1);
  }
  async function submit() {
    setConfirm(false); setBusy(true); setError('');
    await new Promise(resolve => setTimeout(resolve, 350));
    try {
      if (response === 'error') throw new Error('模拟导入服务暂不可用。未生成批次，请重试。');
      commit({ ...state, imports: state.imports + 1 });
      setBatch(state.imports + 1); setStep(4);
    } catch (cause) { setError((cause as Error).message); }
    finally { setBusy(false); }
  }
  return <><PageHeading title="文件接入" subtitle="模拟环境 · 实际文件导入服务尚未接入" /><Steps titles={['选择文件', '字段映射', '质量校验', '预览确认', '导入结果']} current={step} />
    <section className="sp-section sp-workflow" aria-busy={busy}>
      {step === 0 && <><h2>选择数据来源</h2><div className="sp-form-grid"><label>接入方式<select value={mode} onChange={event => { setMode(event.target.value); setError(''); }}><option value="demo">模拟库存样例</option><option value="file">实际文件</option></select></label><label>数据集<input value="库存" readOnly /></label></div><p>模拟库存样例 · {rows.length} 条记录 · {factory === 'all' ? '全部工厂' : `${factory} 工厂`}</p></>}
      {step === 1 && <><h2>字段映射</h2><div className="sp-form-grid">{Object.entries({ material: '物料编码', quantity: '库存数量', unit: '库存单位' }).map(([key, label]) => <label key={key}>{label}<select value={mapping[key as keyof typeof mapping]} onChange={event => { setMapping({ ...mapping, [key]: event.target.value }); setAck(false); }}><option value="">请选择来源列</option>{['物料编码', '数量', '单位'].map(column => <option key={column}>{column}</option>)}</select></label>)}</div></>}
      {step === 2 && <><h2>质量校验 · 模拟结果</h2><StatusTag tone={blocked ? 'warning' : 'success'}>{blocked ? 'P012 数量尚不可计算' : '模拟校验通过'}</StatusTag><p>{blocked ? 'P012 原始库存为 20 箱，缺少经确认的箱 / 件换算关系。' : `依据模拟标准化版本 v${state.repair.version}。`}</p>{blocked && <><ScopedLink to="/data/governance/issues/ISS-012">处理单位换算问题<ArrowRight size={15} /></ScopedLink><label className="sp-checkbox"><input type="checkbox" checked={ack} onChange={event => setAck(event.target.checked)} />保留原始记录并隔离 P012，不用于库存数量判断</label></>}</>}
      {step === 3 && <><h2>预览确认 · 模拟样例</h2><DataTable caption="模拟导入预览" headers={['物料', '原始库存', '标准化数量']}>{rows.map(material => <tr key={material}><td>{material}</td><td>{material === 'P012' ? '20 箱' : '400 件'}</td><td>{material === 'P012' ? repaired ? `${20 * Number(state.repair.factor)} 件` : '尚不可计算（隔离）' : '400 件'}</td></tr>)}</DataTable><div className="sp-form-grid"><ResponseChoice value={response} onChange={setResponse} /></div></>}
      {step === 4 && <><h2>模拟批次 SIM-{String(batch).padStart(3, '0')}</h2><StatusTag tone={blocked ? 'warning' : 'info'}>模拟处理完成 · 非真实导入</StatusTag><dl className="sp-facts"><div><dt>样例记录</dt><dd>{rows.length} 条</dd></div><div><dt>隔离记录</dt><dd>{blocked ? 1 : 0} 条</dd></div><div><dt>标准化版本</dt><dd>模拟 v{state.repair.version}</dd></div><div><dt>实际文件</dt><dd>未上传、未导入</dd></div></dl><ScopedLink to="/data/preview/inventory">查看模拟库存预览<ArrowRight size={15} /></ScopedLink></>}
      <Feedback error={error} />
      <div className="sp-actions">{step > 0 && step < 4 && <Button disabled={busy} onClick={() => { setStep(step - 1); setError(''); }}><ArrowLeft size={15} />上一步</Button>}{step < 3 && <Button primary onClick={next}>下一步<ArrowRight size={15} /></Button>}{step === 3 && <Button primary disabled={busy} onClick={() => setConfirm(true)}><FileInput size={16} />{busy ? '模拟处理中…' : '提交模拟导入'}</Button>}{step === 4 && <Button onClick={() => { setStep(0); setAck(false); }}><RotateCcw size={15} />新建模拟导入</Button>}</div>
    </section>{confirm && <Confirmation title="确认模拟导入" onCancel={() => setConfirm(false)} onConfirm={submit}><p>{rows.length} 条样例记录，{blocked ? 'P012 将被隔离，数量仍不可计算。' : '使用已确认的模拟标准化版本。'}本次操作仅保存浏览器会话内的模拟记录。</p></Confirmation>}</>;
}

export function IssueWorkflow({ mappingPage = false }: { mappingPage?: boolean }) {
  const { state, commit } = useSimulation();
  const [draft, setDraft] = useState({ ...state.repair });
  const [step, setStep] = useState(mappingPage ? 1 : state.repair.checked ? 2 : 0);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [response, setResponse] = useState('ok');
  const dirty = JSON.stringify(draft) !== JSON.stringify(state.repair);
  function edit(key: keyof Repair, value: string | boolean) { setDraft({ ...draft, [key]: value, confirmed: key === 'confirmed' ? Boolean(value) : false }); setError(''); setMessage(''); }
  function validate() {
    if (!draft.owner.trim() || !draft.note.trim()) return '请填写物料负责人和确认任务说明。';
    if (step > 0 && (!Number.isFinite(Number(draft.factor)) || Number(draft.factor) <= 0 || Number(draft.factor) > 1000000)) return '每箱件数须为大于 0 且不超过 1,000,000 的有效数字。';
    if (step > 0 && (!draft.evidence.trim() || !draft.confirmed)) return '请填写换算依据，并确认物料负责人已核对。';
    return '';
  }
  function saveAssignment() {
    const problem = validate(); if (problem) return setError(problem);
    try { commit({ ...state, repair: { ...state.repair, owner: draft.owner.trim(), note: draft.note.trim() } }); setStep(1); setMessage('模拟确认任务已记录，等待填写负责人确认后的换算关系。'); }
    catch (cause) { setError((cause as Error).message); }
  }
  async function recheck() {
    setConfirm(false); setBusy(true); setError('');
    await new Promise(resolve => setTimeout(resolve, 350));
    try {
      if (response === 'error') throw new Error('模拟复检服务暂不可用，版本未保存，问题仍未解决。请重试。');
      const repair = { ...draft, owner: draft.owner.trim(), note: draft.note.trim(), evidence: draft.evidence.trim(), version: state.repair.version + 1, checked: true };
      commit({ ...state, repair }); setDraft(repair); setStep(2); setMessage('模拟复检通过，真实问题状态仍待后端判定。');
    } catch (cause) { setError((cause as Error).message); }
    finally { setBusy(false); }
  }
  return <section className="sp-section sp-workflow" data-unsaved={dirty ? 'true' : 'false'} aria-busy={busy}><h2>{mappingPage ? 'P012 · 箱 / 件换算' : '问题处理 · ISS-012'}</h2><StatusTag tone={state.repair.checked ? 'info' : 'warning'}>{state.repair.checked ? `模拟复检通过 · v${state.repair.version}` : state.repair.owner ? '模拟待负责人确认' : '待确认 · 数量尚不可计算'}</StatusTag><Steps titles={['交给负责人确认', '填写标准与映射', '修复与复检结果']} current={step} />
    {step === 0 && <><dl className="sp-facts"><div><dt>原始库存</dt><dd>20 箱 · B 工厂</dd></div><div><dt>影响事件</dt><dd>EVT-012 · 库存数量判断受阻</dd></div></dl><div className="sp-form-grid"><label>物料负责人<input value={draft.owner} maxLength={80} onChange={event => edit('owner', event.target.value)} required /></label><label>确认任务说明<textarea value={draft.note} maxLength={1000} onChange={event => edit('note', event.target.value)} required rows={3} /></label></div><p>模拟确认任务仅保存在当前浏览器会话，未发送通知。</p></>}
    {step === 1 && <><div className="sp-form-grid"><label>物料负责人<input value={draft.owner} maxLength={80} onChange={event => edit('owner', event.target.value)} required /></label><label>确认任务说明<input value={draft.note} maxLength={1000} onChange={event => edit('note', event.target.value)} required /></label><label>每箱件数<input type="number" min="0.000001" max="1000000" step="any" value={draft.factor} onChange={event => edit('factor', event.target.value)} required /></label><label>换算依据<textarea value={draft.evidence} maxLength={1000} onChange={event => edit('evidence', event.target.value)} rows={3} required /></label></div><label className="sp-checkbox"><input type="checkbox" checked={draft.confirmed} onChange={event => edit('confirmed', event.target.checked)} />物料负责人已确认本次箱 / 件换算依据</label><p>适用范围：P012 · B 工厂 · 箱 → 件</p><div className="sp-form-grid"><ResponseChoice value={response} onChange={setResponse} /></div></>}
    {step === 2 && <><h2>修复与复检结果 · 模拟</h2><dl className="sp-facts"><div><dt>标准化版本</dt><dd>模拟 v{state.repair.version}</dd></div><div><dt>库存数量</dt><dd>20 箱 × {state.repair.factor} = {20 * Number(state.repair.factor)} 件</dd></div><div><dt>依据</dt><dd>{state.repair.evidence}</dd></div><div><dt>负责人</dt><dd>{state.repair.owner}</dd></div></dl><p>真实标准版本未保存，EVT-012 未关闭。后端接入后需重新执行真实复检。</p><ScopedLink to="/risks/EVT-012?tab=evidence">查看受影响事件<ArrowRight size={15} /></ScopedLink><p><ScopedLink to="/data/preview/inventory">查看标准化对照</ScopedLink></p></>}
    <Feedback error={error} message={message} /><div className="sp-actions">{step > 0 && <Button disabled={busy} onClick={() => { setStep(step - 1); setError(''); setMessage(''); }}><ArrowLeft size={15} />上一步</Button>}{step === 0 && <Button primary onClick={saveAssignment}><Save size={15} />记录模拟确认任务并继续</Button>}{step === 1 && <Button primary disabled={busy} onClick={() => { const problem = validate(); setError(problem); if (!problem) setConfirm(true); }}><Check size={15} />{busy ? '模拟复检中…' : '提交修复与模拟复检'}</Button>}</div>
    {confirm && <Confirmation title="确认修复与模拟复检" onCancel={() => setConfirm(false)} onConfirm={recheck}><dl className="sp-facts"><div><dt>确认关系</dt><dd>1 箱 = {draft.factor} 件</dd></div><div><dt>新版本</dt><dd>模拟 v{state.repair.version + 1}</dd></div><div><dt>适用范围</dt><dd>P012 · B 工厂</dd></div><div><dt>影响事件</dt><dd>EVT-012</dd></div></dl><p>本次只生成模拟结果，不会关闭真实风险事件。</p></Confirmation>}
  </section>;
}

export function ApprovalWorkflow() {
  const { state, commit } = useSimulation();
  const [decision, setDecision] = useState('批准');
  const [comment, setComment] = useState('');
  const [ack, setAck] = useState(false);
  const [error, setError] = useState('');
  const [confirm, setConfirm] = useState(false);
  return <section className="sp-section sp-workflow"><h2>审批处理 · 模拟</h2>{state.approval ? <><StatusTag tone="info">模拟已{state.approval.decision} · 真实方案仍待审批</StatusTag><p>{state.approval.comment}</p><p>未触发调拨执行。</p></> : <><div className="sp-form-grid"><label>审批意见<select value={decision} onChange={event => setDecision(event.target.value)}><option>批准</option><option>驳回</option></select></label><label>意见说明<textarea value={comment} onChange={event => setComment(event.target.value)} maxLength={1000} rows={3} /></label></div><label className="sp-checkbox"><input type="checkbox" checked={ack} onChange={event => setAck(event.target.checked)} />已核对 APR-001 绑定的方案 v1，成本和库存仍待业务确认</label><Feedback error={error} /><Button primary onClick={() => { if (!comment.trim() || !ack) setError('请填写意见说明并确认方案版本。'); else { setError(''); setConfirm(true); } }}><Check size={16} />提交模拟审批</Button></>}{confirm && <Confirmation title="确认模拟审批" onCancel={() => setConfirm(false)} onConfirm={() => { setConfirm(false); try { commit({ ...state, approval: { decision, comment: comment.trim() } }); } catch (cause) { setError((cause as Error).message); } }}><p>APR-001 · 方案 v1 · {decision}。本次不调用真实审批或执行服务。</p></Confirmation>}</section>;
}
