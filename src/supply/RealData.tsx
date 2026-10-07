import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { ArrowLeft, ArrowRight, Check, FileInput, RotateCcw } from 'lucide-react';
import { Button, DataTable, PageHeading, ScopedLink, StatusTag } from './components';
import { dataFileUrl, dataRequest, jsonRequest, type Batch, type Change, type Check as CheckResult, type Issue, type MaterialRule, type Preview, type Rule } from './dataApi';
import type { Factory } from './demo';
import { useBusinessAccess } from './BusinessAccess';

function ErrorText({ value }: { value: string }) { return value ? <p role="alert" className="sp-feedback sp-feedback-error">{value}</p> : null; }

export function RealImportWorkflow({ factory, onDemo, onClose, onImported }: { factory: Factory; onDemo?: () => void; onClose?: () => void; onImported?: () => void }) {
  const [step, setStep] = useState(0);
  const [file, setFile] = useState<File | null>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [prepared, setPrepared] = useState<Array<{ preview: Preview; checked: CheckResult; isolate: boolean }>>([]);
  const [batchResults, setBatchResults] = useState<Batch[]>([]);
  const [domain, setDomain] = useState('inventory');
  const [selectedFactory, setFactory] = useState<string>(factory === 'all' ? 'B' : factory);
  const [owner, setOwner] = useState('');
  const [sheet, setSheet] = useState('');
  const [preview, setPreview] = useState<Preview | null>(null);
  const [mapping, setMapping] = useState({ material: '', quantity: '', unit: '', date: '', dateFormat: '%Y-%m-%d' });
  const previousMapping = useRef<{ columns: string[]; mapping: typeof mapping } | null>(null);
  const [checked, setChecked] = useState<CheckResult | null>(null);
  const [isolate, setIsolate] = useState(false);
  const [strategy, setStrategy] = useState('insert');
  const [batch, setBatch] = useState<Batch | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [key, setKey] = useState(() => crypto.randomUUID());
  const dialog = useRef<HTMLDialogElement>(null);
  const embedded = !!onClose;
  const multiple = files.length > 1;
  const hasNextFile = prepared.length + 1 < files.length;
  function selectFiles(selected: File[]) {
    if (selected.length > 20 || selected.some(item => !/\.(csv|xlsx)$/i.test(item.name) || !item.size || item.size > 10 * 1024 * 1024)) {
      setError('每批最多 20 个文件，每个须为非空 CSV / XLSX 且不超过 10 MB。');
      setFiles([]); setFile(null); return;
    }
    setFiles(selected); setFile(selected[0] || null); setPreview(null); setChecked(null); setSheet(''); setError('');
  }
  function nextFile() {
    if (!preview || !checked || checked.blocking || (checked.isolated && !isolate)) return;
    previousMapping.current = { columns: preview.columns, mapping };
    setPrepared([...prepared, { preview, checked, isolate }]);
    setFile(files[prepared.length + 1]); setPreview(null); setChecked(null); setIsolate(false); setSheet(''); setStep(0); setError('');
  }
  useLayoutEffect(() => {
    if (!embedded) return;
    const previous = document.activeElement as HTMLElement;
    const previousOverflow = document.body.style.overflow;
    const element = dialog.current;
    element?.showModal();
    document.body.style.overflow = 'hidden';
    return () => { element?.close(); document.body.style.overflow = previousOverflow; if (previous?.isConnected) previous.focus(); };
  }, [embedded]);
  function close() {
    if (busy) return;
    if (step !== 4 && (file || owner || domain !== 'inventory' || selectedFactory !== (factory === 'all' ? 'B' : factory)) && !window.confirm('当前有未提交的导入内容，关闭后将丢弃。确定关闭？')) return;
    onClose?.();
  }

  async function upload(targetSheet = sheet) {
    if (!file || !owner.trim() || !['A', 'B'].includes(selectedFactory)) return setError('请选择文件、工厂并填写负责人。');
    const form = new FormData();
    form.set('file', file); form.set('domain', domain); form.set('factory', selectedFactory); form.set('owner', owner.trim()); form.set('sheet', targetSheet);
    setBusy(true); setError('');
    try {
      const result = await dataRequest<Preview>('/imports/preview', { method: 'POST', body: form });
      setPreview(result);
      const find = (names: string[]) => result.columns.find(column => names.includes(column.toLowerCase())) || '';
      setMapping(previousMapping.current && JSON.stringify(previousMapping.current.columns) === JSON.stringify(result.columns) ? previousMapping.current.mapping : { material: find(['物料编码', '料号', 'material', 'sku']), quantity: find(['数量', '库存数量', 'quantity']), unit: find(['单位', '库存单位', 'unit']), date: '', dateFormat: '%Y-%m-%d' });
      setChecked(null); setStep(1);
    } catch (cause) { setError((cause as Error).message); }
    finally { setBusy(false); }
  }

  async function check() {
    if (!preview) return;
    setBusy(true); setError('');
    try { setChecked(await jsonRequest<CheckResult>(`/imports/${preview.draftId}/check`, { mapping })); setIsolate(false); setStep(2); }
    catch (cause) { setError((cause as Error).message); }
    finally { setBusy(false); }
  }

  async function submit() {
    if (!preview || !checked || busy || hasNextFile) return;
    setConfirm(false); setBusy(true); setError('');
    try {
      if (multiple) {
        const results = await jsonRequest<Batch[]>('/imports/bulk-commit', { strategy, idempotencyKey: key,
          files: [...prepared, { preview, checked, isolate }].map(item => ({ draftId: item.preview.draftId, isolateWarnings: item.isolate })) });
        setBatchResults(results);
      } else {
        const result = await jsonRequest<Batch>(`/imports/${preview.draftId}/commit`, { strategy, businessKey: 'material', isolateWarnings: isolate, idempotencyKey: key });
        setBatch(result);
      }
      setStep(4); onImported?.();
    } catch (cause) { setError((cause as Error).message); }
    finally { setBusy(false); }
  }

  const reset = () => { setStep(0); setFile(null); setFiles([]); setPrepared([]); setBatchResults([]); previousMapping.current = null; setSheet(''); setPreview(null); setChecked(null); setBatch(null); setKey(crypto.randomUUID()); setError(''); };
  const content = <>{!embedded && <PageHeading title="文件接入" subtitle="真实文件导入" />}<ol className="sp-steps">{['选择来源', '预览与映射', '质量校验', '确认导入', '导入结果'].map((title, index) => <li key={title} aria-current={step === index ? 'step' : undefined}><span>{index + 1}</span>{title}</li>)}</ol>
    <section className="sp-section sp-workflow" aria-busy={busy}>
      {step === 0 && <><h2>选择来源</h2><div className="sp-form-grid">{!embedded && <label>接入方式<select value="file" onChange={event => { if (event.target.value === 'demo') onDemo?.(); }}><option value="file">实际文件</option>{onDemo && <option value="demo">模拟库存样例</option>}</select></label>}<label>数据域<select disabled={busy || prepared.length > 0} value={domain} onChange={event => setDomain(event.target.value)}><option value="inventory">库存</option><option value="purchase">采购</option></select></label><label>工厂<select disabled={busy || prepared.length > 0} value={selectedFactory} onChange={event => setFactory(event.target.value)}><option value="A">A 工厂</option><option value="B">B 工厂</option></select></label><label>负责人<input disabled={busy || prepared.length > 0} value={owner} onChange={event => setOwner(event.target.value)} /></label></div>{!prepared.length && <label className="sp-file-field">选择文件<input type="file" accept=".csv,.xlsx" multiple={embedded} disabled={busy} onChange={event => selectFiles(Array.from(event.target.files || []))} /><small>{embedded ? '每批最多 20 个文件 · 每个 ' : ''}CSV / XLSX · 最大 10 MB · 最多 10,000 条记录</small></label>}</>}
      {multiple && step < 4 && <><p>共 {files.length} 个文件，当前处理第 {prepared.length + 1} 个。全部成功才保存；任一文件失败，整批不保存。</p><DataTable caption="批量导入文件" headers={['文件', '大小', '状态', '操作']}>{files.map((item, index) => <tr key={index}><td>{item.name}</td><td>{Math.ceil(item.size / 1024)} KB</td><td>{index < prepared.length ? '已校验，待整批提交' : index === prepared.length ? '当前文件' : '待处理'}</td><td>{step === 0 && !prepared.length && <Button disabled={busy} onClick={() => selectFiles(files.filter((_, position) => position !== index))}>移除</Button>}</td></tr>)}</DataTable><Button disabled={busy} onClick={() => { if (window.confirm('重新选择将清除本批预览和校验结果，继续？')) reset(); }}>重新选择文件</Button></>}
      {step === 1 && preview && <><h2>预览与映射</h2><p>{preview.filename} · {preview.total} 条记录</p>{preview.sheets.length > 0 && <label>工作表<select value={preview.sheet} disabled={busy} onChange={event => { setSheet(event.target.value); void upload(event.target.value); }}>{preview.sheets.map(name => <option key={name} value={name}>{name}</option>)}</select></label>}<DataTable caption="原始记录预览" headers={preview.columns}>{preview.sample.map((row, index) => <tr key={index}>{preview.columns.map(column => <td key={column}>{row[column]}</td>)}</tr>)}</DataTable><div className="sp-form-grid">{([['material', '物料编码'], ['quantity', '数量'], ['unit', '单位'], ['date', '日期（可选）']] as const).map(([keyName, label]) => <label key={keyName}>{label}<select value={mapping[keyName]} onChange={event => { setMapping({ ...mapping, [keyName]: event.target.value }); setChecked(null); }}><option value="">请选择来源列</option>{preview.columns.map(column => <option key={column} value={column}>{column}</option>)}</select></label>)}{mapping.date && <label>日期格式<select value={mapping.dateFormat} onChange={event => setMapping({ ...mapping, dateFormat: event.target.value })}><option value="%Y-%m-%d">YYYY-MM-DD</option><option value="%Y/%m/%d">YYYY/MM/DD</option><option value="%Y.%m.%d">YYYY.MM.DD</option></select></label>}</div></>}
      {step === 2 && checked && <><h2>质量校验</h2><p>通过 {checked.rows.length - checked.blocking - checked.isolated} 条 · 警告 {checked.isolated} 条 · 阻断 {checked.blocking} 条</p>{checked.issues.length ? <DataTable caption="质量问题" headers={['行号', '级别', '原始值', '原因与影响']}>{checked.issues.map(issue => <tr key={issue.row}><td>{issue.row}</td><td><StatusTag tone={issue.severity === 'blocking' ? 'error' : 'warning'}>{issue.severity === 'blocking' ? '阻断' : '警告'}</StatusTag></td><td>{issue.originalValue}</td><td>{issue.reason}<small>{issue.impact}</small></td></tr>)}</DataTable> : <StatusTag tone="success">全部通过</StatusTag>}{checked.isolated > 0 && <label className="sp-checkbox"><input type="checkbox" checked={isolate} onChange={event => setIsolate(event.target.checked)} />保留原始记录并隔离尚不可计算的记录</label>}</>}
      {step === 3 && checked && preview && <><h2>确认导入</h2><p>{preview.filename} · {preview.total} 条；可导入 {checked.rows.length - checked.isolated} 条；隔离 {checked.isolated} 条。</p><div className="sp-form-grid"><label>导入策略<select value={strategy} onChange={event => setStrategy(event.target.value)}><option value="insert">只新增，已存在则阻止</option><option value="upsert">按业务键新增或更新</option></select></label><label>业务键<input value="物料编码 + 工厂 + 数据域" readOnly /></label></div><DataTable caption="导入记录摘要" headers={['行号', '物料', '原始数量', '标准化数量', '状态']}>{checked.rows.slice(0, 20).map(row => <tr key={row.row}><td>{row.row}</td><td>{row.material}</td><td>{row.quantity} {row.unit}</td><td>{row.normalizedQuantity ?? '尚不可计算'}</td><td>{row.status === 'valid' ? '可导入' : '隔离'}</td></tr>)}</DataTable></>}
      {step === 3 && multiple && !hasNextFile && preview && checked && <><h3>整批导入汇总</h3><DataTable caption="整批导入汇总" headers={['文件', '通过', '隔离', '阻断']}>{[...prepared, { preview, checked, isolate }].map(item => <tr key={item.preview.draftId}><td>{item.preview.filename}</td><td>{item.checked.rows.length - item.checked.isolated - item.checked.blocking}</td><td>{item.checked.isolated}</td><td>{item.checked.blocking}</td></tr>)}</DataTable></>}
      {step === 4 && batchResults.length > 0 && <><h2>导入结果</h2><StatusTag tone="success">全部 {batchResults.length} 个文件已保存</StatusTag><DataTable caption="批量导入结果" headers={['文件', '已导入', '隔离', '批次']}>{batchResults.map(item => <tr key={item.id}><td>{item.filename}</td><td>{item.imported}</td><td>{item.isolated}</td><td><ScopedLink to={`/data/imports/${item.id}`}>{item.id}</ScopedLink></td></tr>)}</DataTable></>}
      {step === 4 && batch && <><h2>导入结果</h2><StatusTag tone="success">批次 {batch.id} 已保存</StatusTag><p>共 {batch.total} 条 · 已导入 {batch.imported} 条 · 隔离 {batch.isolated} 条</p><ScopedLink to={`/data/imports/${batch.id}`}>查看批次明细<ArrowRight size={15} /></ScopedLink><p><ScopedLink to="/data/governance?tab=issues">查看质量问题<ArrowRight size={15} /></ScopedLink></p></>}
      <ErrorText value={error} /><div className="sp-actions">{step > 0 && step < 4 && <Button disabled={busy} onClick={() => { setStep(step - 1); setError(''); }}><ArrowLeft size={15} />上一步</Button>}{step === 0 && <Button primary disabled={busy} permission="data.write" permissionFactory={selectedFactory} permissionDomain={domain} onClick={() => void upload()}><FileInput size={15} />{busy ? '解析中…' : '预览文件'}</Button>}{step === 1 && <Button primary disabled={busy} permission="data.write" permissionFactory={selectedFactory} permissionDomain={domain} onClick={check}>运行校验<ArrowRight size={15} /></Button>}{step === 2 && <Button primary disabled={busy || checked?.blocking !== 0 || !!(checked?.isolated && !isolate)} onClick={() => setStep(3)}>下一步<ArrowRight size={15} /></Button>}{step === 3 && (hasNextFile ? <Button primary disabled={busy} onClick={nextFile}>确认此文件并继续<ArrowRight size={15} /></Button> : <Button primary disabled={busy} permission="data.write" permissionFactory={selectedFactory} permissionDomain={domain} onClick={() => setConfirm(true)}><Check size={15} />{busy && multiple ? '整批提交中…' : multiple ? '提交整批导入' : '提交导入'}</Button>)}{step === 4 && <Button onClick={reset}><RotateCcw size={15} />新建导入</Button>}</div>
    </section>{confirm && <div className="sp-modal-cover"><section className="sp-dialog sp-inline-dialog" role="dialog" aria-modal="true" aria-label="确认真实导入"><h2>确认真实导入</h2><p>{multiple ? `本批 ${files.length} 个文件统一保存，任一文件失败则整批不保存。` : '提交后会保存原始记录与批次，隔离记录不会参与标准化数量计算。'}</p><div className="sp-actions"><Button onClick={() => setConfirm(false)}>取消</Button><Button primary disabled={busy} permission="data.write" permissionFactory={selectedFactory} permissionDomain={domain} onClick={submit}>确认提交</Button></div></section></div>}</>;
  return embedded ? <dialog ref={dialog} className="sp-dialog sp-import-dialog" aria-labelledby="import-dialog-title" onCancel={event => { event.preventDefault(); if (confirm) setConfirm(false); else close(); }}><div className="sp-section-heading"><h2 id="import-dialog-title">文件导入</h2><Button disabled={busy} onClick={close}>关闭</Button></div>{content}</dialog> : content;
}

export function RealBatch({ id }: { id: string }) {
  const [batch, setBatch] = useState<Batch | null>(null);
  const [error, setError] = useState('');
  const [checking, setChecking] = useState(false);
  useEffect(() => { dataRequest<Batch>(`/imports/${id}`).then(setBatch).catch(cause => setError(cause.message)); }, [id]);
  async function recheck() { setChecking(true); setError(''); try { await jsonRequest(`/imports/${id}/recheck`, {}); setBatch(await dataRequest<Batch>(`/imports/${id}`)); } catch (cause) { setError((cause as Error).message); } finally { setChecking(false); } }
  return <><PageHeading title={batch ? `导入批次 ${batch.id}` : '导入批次'} subtitle={batch && `${batch.filename} · ${batch.factory} 工厂 · ${batch.created_at}`} /><ErrorText value={error} />{batch && <><p>共 {batch.total} 条 · 导入 {batch.imported} 条 · 隔离 {batch.isolated} 条</p><div className="sp-actions"><Button disabled={checking} permission="data.write" permissionFactory={batch.factory} permissionDomain={batch.domain} onClick={recheck}><RotateCcw size={15} />{checking ? '复检中…' : '重新校验'}</Button><a className="sp-button" href={dataFileUrl(batch.id)}>下载来源文件</a></div><DataTable caption="导入记录" headers={['行号', '物料', '原始值', '标准化数量', '状态']}>{batch.records?.map(row => <tr key={row.row_number}><td>{row.row_number}</td><td>{row.material}</td><td>{row.quantity} {row.unit}</td><td>{row.normalized_quantity ?? '尚不可计算'}</td><td>{row.status === 'valid' ? '有效' : '隔离'}</td></tr>)}</DataTable><section className="sp-section"><h2>质量问题</h2>{batch.issues?.map(issue => <p key={issue.id}><ScopedLink to={`/data/governance/issues/${issue.id}`}>{issue.id} · {issue.reason}</ScopedLink></p>)}</section></>}</>;
}

export function RealRecords({ factory }: { factory: Factory }) {
  const [records, setRecords] = useState<Array<{ id: number; batch_id: string; filename: string; factory: string; material: string; quantity: string; unit: string; normalized_quantity: string | null }>>([]);
  const [error, setError] = useState('');
  useEffect(() => { dataRequest<typeof records>(`/records?domain=inventory&factory=${factory}`).then(setRecords).catch(cause => setError(cause.message)); }, [factory]);
  return <section className="sp-section"><h2>已接入库存</h2><ErrorText value={error} />{records.length ? <DataTable caption="真实库存数据" headers={['物料', '工厂', '原始值', '标准化数量', '来源']}>{records.map(record => <tr key={record.id}><td>{record.material}</td><td>{record.factory}</td><td>{record.quantity} {record.unit}</td><td>{record.normalized_quantity ?? '尚不可计算'}</td><td><ScopedLink to={`/data/imports/${record.batch_id}`}>{record.filename}</ScopedLink></td></tr>)}</DataTable> : !error && <p>暂无已接入库存。</p>}</section>;
}

export function RealBatchList({ factory }: { factory: Factory }) {
  const [batches, setBatches] = useState<Batch[]>([]);
  const [error, setError] = useState('');
  useEffect(() => { dataRequest<Batch[]>(`/imports?factory=${factory}`).then(setBatches).catch(cause => setError(cause.message)); }, [factory]);
  return <><ErrorText value={error} />{batches.length ? <DataTable caption="真实导入批次" headers={['批次', '文件 / 工厂', '结果', '时间']}>{batches.map(batch => <tr key={batch.id}><td><ScopedLink to={`/data/imports/${batch.id}`}>{batch.id}</ScopedLink></td><td>{batch.filename}<small>{batch.factory} 工厂</small></td><td>导入 {batch.imported} · 隔离 {batch.isolated}</td><td>{batch.created_at}</td></tr>)}</DataTable> : null}</>;
}

export function RealGovernance({ tab, factory }: { tab: string; factory: Factory }) {
  const [issues, setIssues] = useState<Issue[]>([]);
  const [rules, setRules] = useState<Rule[]>([]);
  const [changes, setChanges] = useState<Change[]>([]);
  const [error, setError] = useState('');
  const [status, setStatus] = useState('all');
  const [search, setSearch] = useState('');
  const [refresh, setRefresh] = useState(0);
  const [ruleDraft, setRuleDraft] = useState({ material: '', factory: factory === 'all' ? 'B' : factory, fromUnit: '箱', factor: '', evidence: '', confirmedBy: '', validFrom: new Date().toISOString().slice(0, 10), confirmed: false });
  const [savingRule, setSavingRule] = useState(false);
  useEffect(() => {
    setError('');
    Promise.all([dataRequest<Issue[]>(`/governance/issues?factory=${factory}&status=${status}`), dataRequest<Rule[]>(`/governance/rules?factory=${factory}`), dataRequest<Change[]>(`/governance/changes?factory=${factory}`)])
      .then(([foundIssues, foundRules, foundChanges]) => { setIssues(foundIssues); setRules(foundRules); setChanges(foundChanges); })
      .catch(cause => setError(cause.message));
  }, [factory, status, refresh]);
  async function saveRule() { setSavingRule(true); setError(''); try { await jsonRequest('/governance/rules', ruleDraft); setRefresh(refresh + 1); setRuleDraft({ ...ruleDraft, factor: '', evidence: '', confirmed: false }); } catch (cause) { setError((cause as Error).message); } finally { setSavingRule(false); } }
  return <section className="sp-section"><div className="sp-section-heading"><h2>已接入数据</h2><Button onClick={() => setRefresh(refresh + 1)}><RotateCcw size={15} />刷新</Button></div><ErrorText value={error} />
    {tab === 'quality' && <><p>问题 {issues.length} 条；校验在文件映射后运行，修复后由后端复检。</p><ScopedLink to="/data/imports/new">导入并运行检查<ArrowRight size={15} /></ScopedLink><label>搜索质量问题<input value={search} onChange={event => setSearch(event.target.value)} /></label><IssueTable issues={issues.filter(issue => `${issue.material} ${issue.reason} ${issue.rule}`.toLowerCase().includes(search.toLowerCase()))} /></>}
    {tab === 'issues' && <><label>状态筛选<select value={status} onChange={event => setStatus(event.target.value)}><option value="all">全部</option><option value="open">待处理</option><option value="resolved">已解决</option></select></label><IssueTable issues={issues} /></>}
    {tab === 'mapping' && <><MaterialRules factory={factory} />{rules.length ? <DataTable caption="单位换算规则" headers={['物料 / 工厂', '换算', '版本', '生效日期', '依据']}>{rules.map(rule => <tr key={rule.id}><td>{rule.material} · {rule.factory}</td><td>1 {rule.from_unit} = {rule.factor} {rule.to_unit}</td><td>v{rule.version}</td><td>{rule.valid_from}</td><td>{rule.evidence}<small>{rule.confirmed_by}</small></td></tr>)}</DataTable> : <p>暂无已确认的换算规则。</p>}<h3>新增单位换算版本</h3><div className="sp-form-grid"><label>规则物料编码<input value={ruleDraft.material} onChange={event => setRuleDraft({ ...ruleDraft, material: event.target.value })} /></label><label>规则适用工厂<select value={ruleDraft.factory} onChange={event => setRuleDraft({ ...ruleDraft, factory: event.target.value as 'A' | 'B' })}><option value="A">A 工厂</option><option value="B">B 工厂</option></select></label><label>原始单位<input value={ruleDraft.fromUnit} onChange={event => setRuleDraft({ ...ruleDraft, fromUnit: event.target.value })} /></label><label>每原始单位件数<input type="number" min="0.000001" step="any" value={ruleDraft.factor} onChange={event => setRuleDraft({ ...ruleDraft, factor: event.target.value })} /></label><label>规则生效日期<input type="date" value={ruleDraft.validFrom} onChange={event => setRuleDraft({ ...ruleDraft, validFrom: event.target.value })} /></label><label>规则确认人<input value={ruleDraft.confirmedBy} onChange={event => setRuleDraft({ ...ruleDraft, confirmedBy: event.target.value })} /></label><label>规则换算依据<textarea value={ruleDraft.evidence} onChange={event => setRuleDraft({ ...ruleDraft, evidence: event.target.value })} rows={3} /></label></div><label className="sp-checkbox"><input type="checkbox" checked={ruleDraft.confirmed} onChange={event => setRuleDraft({ ...ruleDraft, confirmed: event.target.checked })} />负责人已确认换算依据</label><Button primary disabled={savingRule} permission="data.write" permissionFactory={ruleDraft.factory} permissionDomain="rules" onClick={saveRule}>保存单位规则版本</Button></>}
    {tab === 'lineage' && (changes.length ? <DataTable caption="来源与变更" headers={['时间', '来源 / 批次', '操作', '详情']}>{changes.map(change => <tr key={change.id}><td>{change.created_at}</td><td>{change.filename}<small><ScopedLink to={`/data/imports/${change.batch_id}`}>{change.batch_id}</ScopedLink></small></td><td>{change.kind}</td><td>{change.details}</td></tr>)}</DataTable> : <p>暂无真实导入与变更记录。</p>)}
  </section>;
}

function IssueTable({ issues }: { issues: Issue[] }) { return issues.length ? <DataTable caption="数据质量问题" headers={['问题', '行号 / 原始值', '原因与影响', '状态']}>{issues.map(issue => <tr key={issue.id}><td><ScopedLink to={`/data/governance/issues/${issue.id}`}>{issue.id}</ScopedLink><small>{issue.material} · {issue.factory} 工厂</small></td><td>{issue.row_number}<small>{issue.original_value}</small></td><td>{issue.reason}<small>{issue.impact}</small></td><td><StatusTag tone={issue.status === 'resolved' ? 'success' : 'warning'}>{issue.status === 'resolved' ? '已解决' : '待处理'}</StatusTag></td></tr>)}</DataTable> : <p>当前筛选范围暂无真实质量问题。</p>; }

function MaterialRules({ factory }: { factory: Factory }) {
  const [rules, setRules] = useState<MaterialRule[]>([]);
  const [draft, setDraft] = useState({ sourceCode: '', standardCode: '', factory: factory === 'all' ? 'B' : factory, evidence: '', confirmedBy: '', validFrom: new Date().toISOString().slice(0, 10), confirmed: false });
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const load = () => dataRequest<MaterialRule[]>(`/governance/material-rules?factory=${factory}`).then(setRules).catch(cause => setError(cause.message));
  useEffect(() => { void load(); }, [factory]);
  async function save() { setBusy(true); setError(''); try { await jsonRequest('/governance/material-rules', draft); await load(); setDraft({ ...draft, sourceCode: '', standardCode: '', evidence: '', confirmed: false }); } catch (cause) { setError((cause as Error).message); } finally { setBusy(false); } }
  return <div><h3>物料编码规则</h3><ErrorText value={error} />{rules.length ? <DataTable caption="物料编码规则" headers={['原始编码', '标准编码', '工厂', '版本 / 生效日期', '依据']}>{rules.map(rule => <tr key={rule.id}><td>{rule.source_code}</td><td>{rule.standard_code}</td><td>{rule.factory}</td><td>v{rule.version} · {rule.valid_from}</td><td>{rule.evidence}<small>{rule.confirmed_by}</small></td></tr>)}</DataTable> : <p>暂无物料编码规则。</p>}<div className="sp-form-grid"><label>来源物料编码<input value={draft.sourceCode} onChange={event => setDraft({ ...draft, sourceCode: event.target.value })} /></label><label>标准物料编码<input value={draft.standardCode} onChange={event => setDraft({ ...draft, standardCode: event.target.value })} /></label><label>编码适用工厂<select value={draft.factory} onChange={event => setDraft({ ...draft, factory: event.target.value as 'A' | 'B' })}><option value="A">A 工厂</option><option value="B">B 工厂</option></select></label><label>编码生效日期<input type="date" value={draft.validFrom} onChange={event => setDraft({ ...draft, validFrom: event.target.value })} /></label><label>编码确认人<input value={draft.confirmedBy} onChange={event => setDraft({ ...draft, confirmedBy: event.target.value })} /></label><label>编码依据<textarea value={draft.evidence} onChange={event => setDraft({ ...draft, evidence: event.target.value })} rows={3} /></label></div><label className="sp-checkbox"><input type="checkbox" checked={draft.confirmed} onChange={event => setDraft({ ...draft, confirmed: event.target.checked })} />负责人已确认编码依据</label><Button primary disabled={busy} permission="data.write" permissionFactory={draft.factory} permissionDomain="rules" onClick={save}>保存编码规则版本</Button><p>已有批次可在详情中重新校验以应用新规则。</p></div>;
}

export function RealIssue({ id }: { id: string }) {
  const { allows } = useBusinessAccess();
  const [issue, setIssue] = useState<Issue | null>(null);
  const [assignee, setAssignee] = useState('');
  const [note, setNote] = useState('');
  const [factor, setFactor] = useState('');
  const [evidence, setEvidence] = useState('');
  const [confirmedBy, setConfirmedBy] = useState('');
  const [validFrom, setValidFrom] = useState(new Date().toISOString().slice(0, 10));
  const [confirmed, setConfirmed] = useState(false);
  const [result, setResult] = useState<{ version: number; normalizedQuantity: string } | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const reload = () => dataRequest<Issue>(`/governance/issues/${id}`).then(setIssue).catch(cause => setError(cause.message));
  useEffect(() => { void reload(); }, [id]);
  async function assign() { setBusy(true); setError(''); try { await jsonRequest(`/governance/issues/${id}/assign`, { assignee, note }); await reload(); } catch (cause) { setError((cause as Error).message); } finally { setBusy(false); } }
  async function repair() { setBusy(true); setError(''); try { setResult(await jsonRequest(`/governance/issues/${id}/repair`, { factor, evidence, confirmedBy, validFrom, confirmed })); await reload(); } catch (cause) { setError((cause as Error).message); } finally { setBusy(false); } }
  return <><PageHeading title={issue ? `${issue.material} 质量问题` : '质量问题'} subtitle={issue && `${issue.id} · ${issue.factory} 工厂 · ${issue.filename}`} /><ErrorText value={error} />{issue && <section className="sp-section sp-workflow"><StatusTag tone={issue.status === 'resolved' ? 'success' : 'warning'}>{issue.status === 'resolved' ? '复检通过' : '待处理'}</StatusTag><dl className="sp-facts"><div><dt>原始值</dt><dd>{issue.original_value}</dd></div><div><dt>所在行</dt><dd>{issue.row_number}</dd></div><div><dt>原因</dt><dd>{issue.reason}</dd></div><div><dt>业务影响</dt><dd>{issue.impact}</dd></div></dl>{issue.status === 'open' && <><h2>分配负责人</h2><div className="sp-form-grid"><label>物料负责人<input value={assignee || issue.assignee || ''} onChange={event => setAssignee(event.target.value)} /></label><label>确认任务说明<input value={note || issue.note || ''} onChange={event => setNote(event.target.value)} /></label></div><Button disabled={busy} permission="data.write" permissionFactory={issue.factory} permissionDomain={issue.domain} onClick={assign}>保存分配</Button>{issue.rule === 'unit_conversion' && issue.assignee && <><h2>标准与映射</h2><p>适用范围：{issue.material} · {issue.factory} 工厂 · {issue.unit} → 件</p><div className="sp-form-grid"><label>每{issue.unit}件数<input type="number" min="0.000001" step="any" value={factor} onChange={event => setFactor(event.target.value)} /></label><label>生效日期<input type="date" value={validFrom} onChange={event => setValidFrom(event.target.value)} /></label><label>确认人<input value={confirmedBy} onChange={event => setConfirmedBy(event.target.value)} /></label><label>换算依据<textarea value={evidence} onChange={event => setEvidence(event.target.value)} rows={3} /></label></div><label className="sp-checkbox"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} />物料负责人已确认换算依据</label><Button primary disabled={busy || !allows('data.write', [issue.factory], 'rules')} permission="data.write" permissionFactory={issue.factory} permissionDomain={issue.domain} onClick={repair}><Check size={15} />提交修复并复检</Button></>}</>}{result && <p role="status">标准化 v{result.version} · {result.normalizedQuantity} 件；问题已解决。</p>}<p><ScopedLink to={`/data/imports/${issue.batch_id}`}>查看来源批次<ArrowRight size={15} /></ScopedLink></p></section>}</>;
}

export function RealImpact() {
  const [issues, setIssues] = useState<Issue[]>([]);
  useEffect(() => { dataRequest<Issue[]>('/governance/issues?factory=B').then(setIssues).catch(() => {}); }, []);
  const related = issues.filter(issue => issue.material === 'P012');
  return related.length ? <section className="sp-section"><h2>已接入数据的质量记录</h2>{related.map(issue => <p key={issue.id}><ScopedLink to={`/data/governance/issues/${issue.id}`}>{issue.id} · {issue.status === 'resolved' ? '复检通过' : '待处理'} · {issue.reason}</ScopedLink></p>)}</section> : null;
}
