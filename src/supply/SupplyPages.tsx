import { useRef, useState } from 'react';
import { Navigate, useLocation, useOutletContext, useParams, useSearchParams } from 'react-router-dom';
import { Activity, ArrowLeft, ArrowRight, ArrowLeftRight, CheckSquare, Database, Download, MessageSquare, MoreHorizontal, Plus, Search, Send, ShieldAlert, Sparkles, Upload } from 'lucide-react';
import { Button, DataTable, EmptyState, PageHeading, ScopedLink, StatusTag } from './components';
import { approvals, datasets, factoryNames, inScope, risks, runs } from './demo';
import Assistant, { Composer } from './Assistant';
import type { SupplyContext } from './SupplyShell';
import { ApprovalWorkflow, ImportWorkflow, IssueWorkflow, useSimulation } from './Workflows';
import { RealBatch, RealBatchList, RealGovernance, RealImpact, RealImportWorkflow, RealIssue, RealRecords } from './RealData';
import DatabaseConnection from './DatabaseConnection';
import RiskWorkspace, { CalculatedRiskDetail } from './RiskWorkspace';
import { BusinessApprovals } from './InvestigationPanel';
import RolePermissions, { ApprovalPolicies } from './RolePermissions';

const riskTabs = { overview: '事件概览', evidence: '调查与证据', plans: '方案比较', execution: '执行追踪', history: '历史记录' };
const governanceTabs = { quality: '质量检查', mapping: '标准与映射', issues: '问题处理', lineage: '来源与变更' };
const settingsTabs = { monitoring: '监测规则', approvals: '审批规则', notifications: '通知设置', permissions: '角色与权限', simulation: '模拟环境' };

function Tabs({ tabs, fallback }: { tabs: Record<string, string>; fallback: string }) {
  const [search, setSearch] = useSearchParams();
  const current = search.get('tab') in tabs ? search.get('tab') : fallback;
  return <nav className="sp-tabs" aria-label="页面页签">{Object.entries(tabs).map(([key, title]) => <button key={key} aria-current={current === key ? 'page' : undefined} onClick={() => { const next = new URLSearchParams(search); next.set('tab', key); setSearch(next); }} className={current === key ? 'is-active' : ''}>{title}</button>)}</nav>;
}

function DetailBack({ fallback, label }: { fallback: string; label: string }) {
  const [search] = useSearchParams();
  const from = search.get('from');
  const fromPath = from?.split('?')[0];
  const target = fromPath === fallback.split('?')[0] || fromPath === '/workspace' ? from! : fallback;
  return <ScopedLink to={target} className="sp-back"><ArrowLeft size={16} />{label}</ScopedLink>;
}

function RiskDetail() {
  const { state: simulation } = useSimulation();
  const { eventId } = useParams();
  const { factory } = useOutletContext<SupplyContext>();
  const [search] = useSearchParams();
  const item = risks.find(item => item.id === eventId && inScope(factory, item));
  const tab = search.get('tab') in riskTabs ? search.get('tab') : 'overview';
  if (!item) return <MissingObject />;
  return <><DetailBack fallback="/risks" label="返回列表" /><PageHeading title={`${item.material} · ${item.name}`} subtitle={`${item.id} · ${factoryNames[item.factory]} · ${item.owner} · 示例更新：2026-10-02 09:18`} action={<StatusTag tone={item.tone}>{item.status}</StatusTag>} />
    <Tabs tabs={riskTabs} fallback="overview" /><div className="sp-detail-layout"><section className="sp-section">
      {tab === 'overview' && <><h2>业务影响</h2><p className="sp-impact">{item.impact}</p><dl className="sp-facts"><div><dt>最晚处理</dt><dd>{item.deadline}</dd></div><div><dt>负责人</dt><dd>{item.owner}</dd></div><div><dt>优先级</dt><dd>{item.priority}</dd></div><div><dt>依据</dt><dd>模拟场景 · 库存快照 v1</dd></div></dl><h2>下一步</h2>{item.id === 'EVT-012' ? <ScopedLink to="/data/governance/issues/ISS-012">确认单位换算<ArrowRight size={15} /></ScopedLink> : item.id === 'EVT-001' ? <ScopedLink to="/approvals/APR-001">查看方案审批<ArrowRight size={15} /></ScopedLink> : <ScopedLink to="/runs/RUN-008">查看运行记录<ArrowRight size={15} /></ScopedLink>}</>}
      {tab === 'evidence' && <><h2>调查与证据</h2><StatusTag tone="warning">示例依据 · 未连接业务系统</StatusTag><p>库存、采购和生产计划尚需接入及核对。</p><ScopedLink to="/data/preview/inventory">查看示例库存快照<ArrowRight size={15} /></ScopedLink>{item.id === 'EVT-012' && <><p><ScopedLink to="/data/governance/issues/ISS-012">库存单位换算问题与复检记录</ScopedLink></p>{simulation.repair.checked && <p>模拟标准化 v{simulation.repair.version}：20 箱 = {20 * Number(simulation.repair.factor)} 件。真实风险状态仍待后端复检。</p>}<RealImpact /></>}</>}
      {tab === 'plans' && (item.id === 'EVT-001' ? <><h2>P001 跨厂调拨方案 v1</h2><dl className="sp-facts"><div><dt>数量</dt><dd>400 件</dd></div><div><dt>调拨方向</dt><dd>B 工厂 → A 工厂</dd></div><div><dt>成本</dt><dd>待业务核算</dd></div><div><dt>执行条件</dt><dd>审批及库存复查通过</dd></div></dl><ScopedLink to="/approvals/APR-001">查看审批<ArrowRight size={15} /></ScopedLink></> : <EmptyState title="尚无可核对方案"><p>接入数据并完成调查后生成。</p></EmptyState>)}
      {tab === 'execution' && <><h2>执行追踪</h2><StatusTag tone={item.id === 'EVT-008' ? 'info' : 'warning'}>{item.id === 'EVT-008' ? '执行中' : '尚未执行'}</StatusTag><p>审批、任务完成与风险关闭分别记录。</p>{item.id === 'EVT-008' && <ScopedLink to="/runs/RUN-008">查看关联运行任务<ArrowRight size={15} /></ScopedLink>}</>}
      {tab === 'history' && <><h2>历史记录</h2><div className="sp-timeline"><div><small>2026-10-02 09:18 · 示例记录</small><strong>事件状态：{item.status}</strong><p>关联对象：{item.id}</p></div></div></>}
    </section><aside className="sp-event-assistant"><h2><Sparkles size={17} />事件助手</h2><p>{item.id} · {factoryNames[item.factory]}</p><Composer contextKey={item.id} /></aside></div>
  </>;
}

function Approvals() {
  const { approvalId } = useParams();
  const { factory } = useOutletContext<SupplyContext>();
  const items = approvals.filter(item => inScope(factory, item));
  if (approvalId) {
    const item = items.find(item => item.id === approvalId);
    if (!item) return <MissingObject />;
    return <><DetailBack fallback="/approvals" label="返回审批列表" /><PageHeading title={item.title} subtitle={`${item.id} · 绑定方案版本 ${item.version}`} action={<StatusTag tone="warning">待审批</StatusTag>} /><section className="sp-section"><h2>审批内容</h2><dl className="sp-facts"><div><dt>数量</dt><dd>400 件</dd></div><div><dt>范围</dt><dd>B 工厂 → A 工厂</dd></div><div><dt>成本</dt><dd>待业务核算</dd></div><div><dt>数据依据</dt><dd>示例库存快照 v1</dd></div></dl><ScopedLink to={`/risks/${item.event}?tab=plans`}>查看事件与方案<ArrowRight size={15} /></ScopedLink><p><StatusTag tone="warning">审批服务尚未接入</StatusTag></p></section><ApprovalWorkflow /></>;
  }
  return <><PageHeading title="审批中心" subtitle="审批对象与具体方案版本关联" />{items.length ? <DataTable caption="示例审批记录" headers={['审批 / 方案', '方案版本', '状态', '操作']}>{items.map(item => <tr key={item.id}><td><strong>{item.title}</strong><small>{item.id}</small></td><td>{item.version}</td><td><StatusTag tone="warning">{item.status}</StatusTag></td><td><ScopedLink to={`/approvals/${item.id}`}>查看审批<ArrowRight size={14} /></ScopedLink></td></tr>)}</DataTable> : <EmptyState title="当前工厂无示例审批记录" />}</>;
}

function DatasetTable() {
  const { factory } = useOutletContext<SupplyContext>();
  return <DataTable caption="数据集就绪状态" headers={['数据集', '范围 / 记录', '状态', '操作']}>{datasets.map(item => <tr key={item.id}><td>{item.title}</td><td>{item.id === 'inventory' ? `${risks.filter(risk => inScope(factory, risk)).length} 条示例记录` : item.count}</td><td><StatusTag tone={item.id === 'inventory' && factory === 'A' ? 'success' : item.tone}>{item.id === 'inventory' && factory === 'A' ? '校验通过' : item.status}</StatusTag></td><td><ScopedLink to={`/data/catalog/${item.id}`}>查看目录<ArrowRight size={14} /></ScopedLink></td></tr>)}</DataTable>;
}

function DataConnections() {
  const { factory } = useOutletContext<SupplyContext>();
  const [importOpen, setImportOpen] = useState(false);
  const [batchVersion, setBatchVersion] = useState(0);
  const [records, setRecords] = useState('all');
  const [notice, setNotice] = useState<'api' | 'template'>('template');
  const dialog = useRef<HTMLDialogElement>(null);
  function showNotice(value: typeof notice) { setNotice(value); dialog.current?.showModal(); }
  const template = '物料编码,数量,单位,日期\r\n';
  return <><div className="sp-heading sp-heading-actions"><a className="sp-button" download="数据导入模板.csv" href={`data:text/csv;charset=utf-8,${encodeURIComponent('\uFEFF' + template)}`}><Download size={16} />下载数据模板</a></div>
    <div className="sp-entry-grid sp-connections-grid">
      <div className="sp-entry sp-connection-card"><Upload size={22} /><strong>文件导入</strong><StatusTag tone="success">可用</StatusTag><p>上传 CSV 或 Excel，接入库存、采购等业务数据。</p><Button primary onClick={() => setImportOpen(true)}>上传文件<ArrowRight size={15} /></Button></div>
      <DatabaseConnection factory={factory} />
      <div className="sp-entry sp-connection-card"><ArrowLeftRight size={22} /><strong>业务接口</strong><StatusTag tone="info">未配置</StatusTag><p>连接业务系统 API，获取交期、运输与收货等数据。</p><Button primary onClick={() => showNotice('api')}>配置接口<ArrowRight size={15} /></Button></div>
    </div>
    <Button onClick={() => showNotice('template')}>查看模板字段说明<ArrowRight size={15} /></Button>
    <section className="sp-section"><h2>导入记录</h2><nav className="sp-tabs" aria-label="导入记录分类">{Object.entries({ all: '全部记录', user: '用户导入', demo: '示例数据' }).map(([key, title]) => <button key={key} aria-current={records === key ? 'page' : undefined} className={records === key ? 'is-active' : ''} onClick={() => setRecords(key)}>{title}</button>)}</nav>
      <div hidden={records === 'demo'}><RealBatchList key={batchVersion} factory={factory} /></div>
    </section>
    {importOpen && <RealImportWorkflow factory={factory} onClose={() => setImportOpen(false)} onImported={() => { setBatchVersion(version => version + 1); setRecords('user'); }} />}
    <dialog ref={dialog} className="sp-dialog" aria-labelledby="connection-notice-title"><h2 id="connection-notice-title">{notice === 'template' ? '模板字段说明' : '配置接口'}</h2>
      {notice === 'template' ? <><p>模板适用于当前库存、采购文件导入。CSV 使用 UTF-8 编码，也可上传 XLSX。</p><dl className="sp-facts"><div><dt>物料编码（必填）</dt><dd>业务物料编号，不能为空。</dd></div><div><dt>数量（必填）</dt><dd>大于或等于 0 的数字。</dd></div><div><dt>单位（必填）</dt><dd>如“件”；其他单位需要对应换算规则。</dd></div><div><dt>日期（可选）</dt><dd>建议 YYYY-MM-DD，上传后选择日期列映射。</dd></div></dl><p>工厂、数据域和负责人在上传页面填写。下载文件仅包含表头，请填写业务数据后上传。</p></> : <p>业务接口当前未配置，配置服务尚未开放。当前可通过文件上传接入业务数据。</p>}
      <div className="sp-actions"><Button onClick={() => dialog.current?.close()}>关闭</Button></div>
    </dialog>
  </>;
}

function DataPages() {
  const { state: simulation } = useSimulation();
  const location = useLocation();
  const { datasetId, issueId, batchId } = useParams();
  const { factory } = useOutletContext<SupplyContext>();
  const [search] = useSearchParams();
  const path = location.pathname;
  const dataset = datasets.find(item => item.id === datasetId);
  if (path === '/data') return <Navigate to={{ pathname: '/data/connections', search: location.search, hash: location.hash }} replace />;
  if (path === '/data/connections') return <DataConnections />;
  if (path === '/data/imports/new') return <ImportWorkflow />;
  if (batchId) return batchId !== 'BATCH-001' ? <RealBatch id={batchId} /> : factory !== 'A' ? <><DetailBack fallback="/data/connections" label="返回数据接入" /><PageHeading title="库存导入批次 BATCH-001" subtitle="模拟批次 · 原始库存快照 v1" /><StatusTag tone={simulation.repair.checked ? 'info' : 'warning'}>{simulation.repair.checked ? `模拟复检通过 · 标准化 v${simulation.repair.version}` : '存在单位换算问题'}</StatusTag><section className="sp-section"><h2>校验结果</h2><p>{simulation.repair.checked ? `P012 模拟换算：20 箱 = ${20 * Number(simulation.repair.factor)} 件。真实问题仍待后端判定。` : 'P012 箱 / 件换算缺失，数量尚不可计算。'}</p><ScopedLink to="/data/governance/issues/ISS-012">查看质量问题</ScopedLink><p><ScopedLink to="/data/preview/inventory">查看快照</ScopedLink></p></section></> : <MissingObject />;
  if (path === '/data/catalog') return <DatasetTable />;
  if (datasetId) {
    if (!dataset) return <MissingObject />;
    if (path.startsWith('/data/preview/')) return <><DetailBack fallback="/data/catalog" label="返回目录" /><PageHeading title={`${dataset.title} · 数据预览`} subtitle={`${factoryNames[factory]} · 原始示例快照 v1 · 模拟标准化 v${simulation.repair.version}`} />{dataset.id === 'inventory' ? <><RealRecords factory={factory} /><h2>模拟样例</h2><DataTable caption="示例库存原始与标准化对照" headers={['物料', '工厂', '原始数量', '原始单位', '标准化数量', '校验状态']}>{risks.filter(item => inScope(factory, item)).map(item => <tr key={item.id}><td>{item.material}</td><td>{factoryNames[item.factory]}</td><td>{item.material === 'P012' ? '20' : '400'}</td><td>{item.material === 'P012' ? '箱' : '件'}</td><td>{item.material === 'P012' ? simulation.repair.checked ? `${20 * Number(simulation.repair.factor)} 件（模拟）` : '尚不可计算' : '400 件'}</td><td><StatusTag tone={item.material === 'P012' ? simulation.repair.checked ? 'info' : 'warning' : 'success'}>{item.material === 'P012' ? simulation.repair.checked ? '模拟复检通过' : '缺少换算关系' : '校验通过'}</StatusTag></td></tr>)}</DataTable></> : <EmptyState title="数据尚未接入" />}</>;
    return <><DetailBack fallback="/data/catalog" label="返回目录" /><PageHeading title={dataset.title} subtitle={`${dataset.id} · ${dataset.count}`} action={<StatusTag tone={dataset.tone}>{dataset.status}</StatusTag>} /><section className="sp-section"><h2>数据口径</h2><p>来源：模拟场景。负责人：数据管理组。</p><p>工厂和物料编码按原始记录保留；缺少单位换算时不得推算库存数量。</p><ScopedLink to={`/data/preview/${dataset.id}`}>查看数据预览<ArrowRight size={15} /></ScopedLink></section></>;
  }
  if (issueId) return issueId !== 'ISS-012' ? <RealIssue id={issueId} /> : factory !== 'A' ? <><DetailBack fallback="/data/governance?tab=issues" label="返回数据治理" /><PageHeading title="P012 库存单位缺少换算关系" subtitle="ISS-012 · B 工厂 · 示例数据" /><IssueWorkflow /><ScopedLink to="/data/governance?tab=mapping">打开标准与映射<ArrowRight size={15} /></ScopedLink></> : <MissingObject />;
  const tab = search.get('tab') in governanceTabs ? search.get('tab') : 'quality';
  if (tab === 'mapping' && factory !== 'A') return <><PageHeading title="数据治理" /><Tabs tabs={governanceTabs} fallback="quality" /><RealGovernance tab={tab} factory={factory} /><h2>模拟样例</h2><IssueWorkflow mappingPage /></>;
  return <><PageHeading title="数据治理" /><Tabs tabs={governanceTabs} fallback="quality" /><RealGovernance tab={tab} factory={factory} /><section className="sp-section"><h2>模拟样例 · {governanceTabs[tab]}</h2>{factory === 'A' ? <EmptyState title="当前工厂无示例质量问题" /> : <><p><StatusTag tone={simulation.repair.checked ? 'info' : 'warning'}>{simulation.repair.checked ? `P012 模拟复检通过 · v${simulation.repair.version}` : simulation.repair.owner ? `模拟待确认 · ${simulation.repair.owner}` : 'P012 单位换算检查待确认'}</StatusTag></p>{tab === 'lineage' && <><p>模拟库存文件 → BATCH-001 → 原始快照 v1 → 模拟标准化 v{simulation.repair.version} → EVT-012</p>{simulation.repair.checked && <p>确认人：{simulation.repair.owner} · 依据：{simulation.repair.evidence} · 原始快照保留</p>}</>}<ScopedLink to="/data/governance/issues/ISS-012">处理问题 ISS-012<ArrowRight size={15} /></ScopedLink></>}</section></>;
}

function Runs() {
  const { runId } = useParams();
  const { factory } = useOutletContext<SupplyContext>();
  const [search] = useSearchParams();
  const items = runs.filter(item => inScope(factory, item) && (!search.get('status') || item.status === search.get('status')));
  if (runId) {
    const item = runs.find(item => item.id === runId && inScope(factory, item));
    if (!item) return <MissingObject />;
    return <><DetailBack fallback="/runs" label="返回运行列表" /><PageHeading title={item.title} subtitle={`${item.id} · 示例运行记录`} action={<StatusTag tone={item.status === '运行中' ? 'info' : 'warning'}>{item.status}</StatusTag>} /><section className="sp-section"><h2>运行轨迹</h2><div className="sp-timeline"><div><small>2026-10-02 09:12</small><strong>读取示例库存快照 v1</strong></div><div><small>2026-10-02 09:18</small><strong>{item.status === '等待数据' ? '等待 P012 单位换算确认' : '跟踪连接器交付进展'}</strong><p>示例记录，尚未调用外部工具。</p></div></div><ScopedLink to={`/risks/${item.event}?tab=execution`}>查看关联事件<ArrowRight size={15} /></ScopedLink></section></>;
  }
  return <><PageHeading title="运行记录" subtitle={`${factoryNames[factory]} · 示例任务轨迹`} />{items.length ? <DataTable caption="示例运行列表" headers={['任务', '关联事件', '状态', '操作']}>{items.map(item => <tr key={item.id}><td>{item.title}<small>{item.id}</small></td><td>{item.event}</td><td><StatusTag tone={item.status === '运行中' ? 'info' : 'warning'}>{item.status}</StatusTag></td><td><ScopedLink to={`/runs/${item.id}`}>查看详情<ArrowRight size={14} /></ScopedLink></td></tr>)}</DataTable> : <EmptyState title="当前范围无示例任务" />}</>;
}

function Settings() {
  const [search] = useSearchParams();
  const tab = search.get('tab') in settingsTabs ? search.get('tab') : 'monitoring';
  if (tab === 'permissions' || tab === 'approvals') return <><PageHeading title="系统设置" /><Tabs tabs={settingsTabs} fallback="monitoring" /><section className="sp-section">{tab === 'permissions' ? <RolePermissions /> : <ApprovalPolicies />}</section></>;
  if (tab === 'monitoring') return <><PageHeading title="系统设置" /><Tabs tabs={settingsTabs} fallback="monitoring" /><p>监测配置与当前工作空间绑定；系统管理员可在风险处置页修改回放范围、自动扫描开关和间隔。</p><ScopedLink to="/risks">管理风险监测</ScopedLink></>;
  return <><PageHeading title="系统设置" /><Tabs tabs={settingsTabs} fallback="monitoring" /><section className="sp-section"><h2>{settingsTabs[tab]}</h2>{tab === 'simulation' ? <><StatusTag tone="info">模拟环境</StatusTag><p>当前供应链页面使用固定示例对象，未连接企业系统。</p></> : <EmptyState title="系统配置服务尚未接入" />}<ScopedLink to="/account">打开账号设置<ArrowRight size={15} /></ScopedLink></section></>;
}
function MissingObject() { return <EmptyState title="对象不存在或不在当前工厂范围"><ScopedLink to="/workspace">返回工作台</ScopedLink></EmptyState>; }

export default function SupplyPages({ notFound = false }: { notFound?: boolean }) {
  const { pathname } = useLocation();
  if (notFound) return <EmptyState title="页面未找到"><ScopedLink to="/workspace">返回工作台</ScopedLink></EmptyState>;
  if (pathname === '/workspace') return <RiskWorkspace overview />;
  if (pathname.startsWith('/assistant')) return <Assistant />;
  if (pathname === '/risks') return <RiskWorkspace />;
  if (pathname.startsWith('/risks/RISK-')) return <CalculatedRiskDetail />;
  if (pathname.startsWith('/risks/')) return <RiskDetail />;
  if (pathname === '/approvals' || pathname.startsWith('/approvals/PLAN-')) return <BusinessApprovals />;
  if (pathname.startsWith('/approvals')) return <Approvals />;
  if (pathname.startsWith('/data')) return <DataPages />;
  if (pathname.startsWith('/runs')) return <Runs />;
  return <Settings />;
}
