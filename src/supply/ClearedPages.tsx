import { useState } from 'react';
import { useLocation, useParams, useSearchParams } from 'react-router-dom';
import { Database, Diamond, ListFilter, Plus, ShieldCheck, Sparkles } from 'lucide-react';
import { Button, DataTable, PageHeading, ScopedLink } from './components';
import { Composer } from './Assistant';
import { useAccess } from '../logistics/LogisticsShell';

// 复用原页面结构与编辑器；旧业务记录已清理，不从 demo 或旧接口回填。
export function ClearedAssistant() {
  const { access } = useAccess();
  const { threadId } = useParams();
  const [suggested, setSuggested] = useState<{text: string}>(), [reset, setReset] = useState(0);
  const [history, setHistory] = useState(false), [scope, setScope] = useState(false);
  const [region, setRegion] = useState(''), [at, setAt] = useState('');
  const regions = [...new Set(access?.roles.flatMap(g => g.regions) || [])];
  const prompts = [
    { title: '调查揽收风险', text: '检查所选区域的揽收时效风险', Icon: Diamond },
    { title: '核对任务数据', text: '核对任务的数据来源与质量', Icon: Database },
    { title: '整理调查依据', text: '整理风险调查所需的证据', Icon: ShieldCheck },
  ];
  return <div className="sp-chat-layout">
    <aside className={`sp-conversations ${history ? 'sp-panel-open' : ''}`} aria-label="历史对话">
      <Button primary className="sp-new-conversation" onClick={() => { if (document.querySelector('[data-unsaved="true"]') && !window.confirm('清空当前草稿并新建对话？')) return; setSuggested({text:''}); setReset(n=>n+1); }}><Plus size={18}/>新建对话</Button>
      <button className="sp-mobile-panel" aria-expanded={history} onClick={()=>setHistory(!history)}>历史对话</button>
      <div className="sp-panel-body"><h2>历史对话</h2><p className="sp-muted">暂无对话记录</p></div>
    </aside>
    <section className="sp-chat" aria-label="聊天分析">
      <div className="sp-chat-topline"><span>新会话</span><button className="sp-button" aria-expanded={scope} onClick={()=>setScope(!scope)}><ListFilter size={14}/>分析范围</button></div>
      <div className="sp-chat-messages sp-chat-empty" aria-label="分析消息">
        <div className="sp-chat-welcome"><div className="sp-welcome-mark"><Sparkles size={28}/></div><h2>今天想调查什么？</h2><p>描述问题、选择范围，保留你的调查草稿。</p>
          {threadId && <p role="status">原会话已清理，可以重新编写问题。</p>}
          <div className="sp-prompt-cards">{prompts.map(({title,text,Icon})=><button className="sp-prompt-card" key={title} onClick={()=>{ if (document.querySelector('[data-unsaved="true"]') && !window.confirm('替换当前草稿？')) return; setSuggested({text}); }}><Icon size={22}/><strong>{title}</strong><span>{text}</span></button>)}</div>
          <div className="sp-welcome-status">当前可编辑草稿；物流调查服务接入后可发送分析。</div>
        </div>
      </div>
      <Composer logistics contextKey="new" suggested={suggested} reset={reset} toolbar={<div className="sp-composer-tools"><ScopedLink to="/data/connections" className="sp-add-data" aria-label="前往数据接入"><Plus size={20}/></ScopedLink><span className="sp-scope-chip">上海 · {region ? `区域 ${region}` : '授权区域'}</span><span className="sp-scope-chip">{at || '时点待选择'}</span></div>}/>
      {scope && <aside className="sp-analysis-scope sp-scope-popover" aria-label="当前分析范围"><h2>当前分析范围</h2><label>区域<select aria-label="分析区域" value={region} onChange={e=>setRegion(e.target.value)}><option value="">全部授权区域</option>{regions.map(r=><option key={r}>{r}</option>)}</select></label><label>参考时点<input type="datetime-local" value={at} onChange={e=>setAt(e.target.value)}/></label><p>仅用于准备问题，尚未创建业务快照。</p><Button onClick={()=>setScope(false)}>关闭范围</Button></aside>}
    </section>
  </div>;
}

export function ClearedBusinessPages() {
  const { pathname } = useLocation();
  const [search,setSearch] = useSearchParams();
  const overview = pathname === '/workspace';
  const approval = pathname.startsWith('/approvals'), run = pathname.startsWith('/runs');
  const title = overview ? '工作台' : approval ? '审批中心' : run ? '运行记录' : '风险处置';
  const tabs = approval ? ['全部','待审核','已批准','已驳回'] : run ? ['全部','运行中','已完成','失败'] : ['全部','待调查','调查中','待跟进','已关闭'];
  const headers = approval ? ['审批 / 建议','方案版本','发起人','状态','操作'] : run ? ['任务','关联事件','状态','开始时间','操作'] : ['风险 / 事件','区域','状态','负责人','更新时间','操作'];
  function filter(key: string,value: string) { const next=new URLSearchParams(search); value?next.set(key,value):next.delete(key);setSearch(next); }
  return <><PageHeading title={title} subtitle="上海物流运营空间" action={<ScopedLink to="/assistant" className="sp-button">打开智能助手</ScopedLink>}/>
    {overview && <><div className="sp-metrics">{['待调查风险','待补充数据','待审核建议','运行任务'].map(label=><div className="sp-metric" key={label}><span>{label}</span><strong>—</strong><small>等待业务服务接入</small></div>)}</div><div className="sp-entry-grid">{[['/assistant','发起调查'],['/data/connections','打开数据管理'],['/approvals','查看审批'],['/runs','查看运行记录']].map(([to,text])=><ScopedLink key={to} to={to} className="sp-entry">{text}</ScopedLink>)}</div></>}
    <section className="sp-section"><div className="sp-section-heading"><h2>{overview?'风险事件':title}</h2><Button disabled title="物流业务服务尚未接入">{approval?'批量审核':run?'刷新记录':'立即扫描'}</Button></div>
      <nav className="sp-tabs" aria-label={`${title}状态筛选`}>{tabs.map(t=><button key={t} aria-current={(search.get('status')||'全部')===t?'page':undefined} className={(search.get('status')||'全部')===t?'is-active':''} onClick={()=>filter('status',t==='全部'?'':t)}>{t}</button>)}</nav>
      <div className="sp-filters"><label>搜索记录<input aria-label="搜索记录" placeholder="编号、名称或负责人" value={search.get('q')||''} onChange={e=>filter('q',e.target.value)}/></label></div>
      <DataTable caption={`${title}列表`} headers={headers}><tr><td colSpan={headers.length}><p>暂无业务记录</p><small>旧模拟记录已清理；物流业务服务接入后在此显示真实记录。</small></td></tr></DataTable>
      <div className="sp-pagination"><span>暂无可分页记录</span><Button disabled>上一页</Button><Button disabled>下一页</Button></div>
    </section>
  </>;
}
