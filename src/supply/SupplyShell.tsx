import AccountMenu from '../logistics/AccountMenu';
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { NavLink, Outlet, useLocation } from 'react-router-dom';
import { Bell, BookOpen, CheckSquare, ChevronRight, Database, LogOut, Menu, Settings, ShieldAlert, Sparkles, UserRound, X } from 'lucide-react';
import { Screen } from '../types';
import { Button, ScopedLink, StatusTag } from './components';
import type { Factory } from './demo';
import { useAccess } from '../logistics/LogisticsShell';
import { useSnapshot } from '../logistics/SnapshotContext';

const navigation = [
  { path: '/assistant', title: '智能助手', icon: Sparkles },
  { path: '/metrics', title: '风险中心', icon: ShieldAlert },
  { path: '/approvals', title: '审批中心', icon: CheckSquare },
  { path: '/knowledge', title: '知识库', icon: BookOpen },
  { path: '/data', title: '数据中心', icon: Database },
];
const dataNavigation = [{ path: '/data/connections', title: '数据接入' }, { path: '/data/catalog', title: '数据目录' }, { path: '/data/governance', title: '数据治理' }, { path: '/data/preview/pickup_tasks', title: '数据预览' }];
export type SupplyContext = { factory: Factory; currentUser: string; headingTarget: HTMLDivElement | null };

export default function SupplyShell({ currentUser, onNavigate }: { currentUser: string; onNavigate: (screen: Screen) => void }) {
  return <AuthorizedShell currentUser={currentUser} onNavigate={onNavigate} />;
}

function AuthorizedShell({ currentUser, onNavigate }: { currentUser: string; onNavigate: (screen: Screen) => void }) {
  const { access } = useAccess();
  const { config, region, setRegion, status } = useSnapshot();
  const location = useLocation();
  const factory: Factory = 'all';
  const [menuOpen, setMenuOpen] = useState(false);
  const [dataMenuOpen, setDataMenuOpen] = useState(() => location.pathname === '/data' || location.pathname.startsWith('/data/'));
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const [headingTarget, setHeadingTarget] = useState<HTMLDivElement | null>(null);
  const menuRef = useRef<HTMLButtonElement>(null);
  const sidebarRef = useRef<HTMLElement>(null);
  const notificationRef = useRef<HTMLButtonElement>(null);
  const scopeSearch = factory === 'all' ? '' : `?factory=${factory}`;
  const module = navigation.find(item => location.pathname === item.path || location.pathname.startsWith(item.path + '/'));
  const isModuleHome = module?.path === location.pathname && module.path !== '/assistant';
  const subPage = dataNavigation.find(item => location.pathname === item.path);
  const parentPath = location.pathname.startsWith('/data/governance/issues/') ? '/data/governance?tab=issues' : location.pathname.startsWith('/data/imports/') ? '/data/connections' : location.pathname.startsWith('/data/preview/') ? '/data' : location.pathname.split('/').slice(0, -1).join('/');

  useEffect(() => { setMenuOpen(false); setNotificationsOpen(false); }, [location.pathname]);
  useEffect(() => {
    if (location.pathname === '/data' || location.pathname.startsWith('/data/')) setDataMenuOpen(true);
  }, [location.pathname]);
  useEffect(() => {
    if (!menuOpen) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const elements = sidebarRef.current?.querySelectorAll<HTMLElement>('a, button');
    elements?.[0]?.focus();
    function trap(event: KeyboardEvent) {
      if (event.key !== 'Tab' || !elements?.length) return;
      const first = elements[0];
      const last = elements[elements.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
    window.addEventListener('keydown', trap);
    return () => { document.body.style.overflow = previousOverflow; window.removeEventListener('keydown', trap); menuRef.current?.focus(); };
  }, [menuOpen]);
  useEffect(() => {
    function close(event: KeyboardEvent) {
      if (event.key !== 'Escape') return;
      if (notificationsOpen) { setNotificationsOpen(false); notificationRef.current?.focus(); }
      else if (menuOpen) { setMenuOpen(false); menuRef.current?.focus(); }
    }
    window.addEventListener('keydown', close);
    return () => window.removeEventListener('keydown', close);
  }, [menuOpen, notificationsOpen]);

  useLayoutEffect(() => {
    const key = `supply-scroll:${currentUser}:${location.pathname}${location.search}`;
    try { window.scrollTo(0, Number(sessionStorage.getItem(key) || 0)); } catch { window.scrollTo(0, 0); }
    const save = () => { try { sessionStorage.setItem(key, String(window.scrollY)); } catch {} };
    window.addEventListener('scroll', save, { passive: true });
    return () => window.removeEventListener('scroll', save);
  }, [location.pathname, location.search, currentUser]);

  return <div className="sp-app">
    <a href="#supply-content" className="sp-skip">跳到主要内容</a>
    <aside ref={sidebarRef} className={`sp-sidebar ${menuOpen ? 'sp-sidebar-open' : ''}`} aria-label="物流导航">
      <ScopedLink to="/assistant" className="sp-brand"><span className="sp-brand-mark"><svg viewBox="0 0 36 44" width="34" height="40" aria-hidden="true"><path fill="#7545ED" d="M21 1 33 8 13 20 3 14Z" /><path fill="#5026BF" d="m3 14 10 6v12L3 26Z" /><path fill="#AA8BFF" d="m13 20 10-6v12l-10 6Z" /><path fill="#8658F3" d="m23 14 10 6v12L23 26Z" /><path fill="#6431D7" d="m33 32-20 12L3 38l20-12Z" /><path fill="#B299FF" d="m3 38 10-6 10 6-10 6Z" /></svg></span><span><strong>Scope</strong><small>物流调查助手</small></span></ScopedLink>
      <nav>{navigation.map(({ path, title, icon: Icon }) => <div key={path}>
        {path === '/data' ? <button type="button" className={`sp-nav-item sp-nav-group ${module?.path === '/data' ? 'is-active' : ''}`} aria-expanded={dataMenuOpen} aria-controls="data-subnav" onClick={() => setDataMenuOpen(open => !open)}><Icon size={18} /><span>{title}</span><ChevronRight size={16} aria-hidden="true" style={{ marginLeft: 'auto', transform: dataMenuOpen ? 'rotate(90deg)' : undefined }} /></button> : <NavLink to={path + scopeSearch} className={({ isActive }) => `sp-nav-item ${isActive ? 'is-active' : ''}`}><Icon size={18} /><span>{title}</span></NavLink>}
        {path === '/data' && dataMenuOpen && <div id="data-subnav" className="sp-subnav">{dataNavigation.map(item => <NavLink key={item.path} to={item.path + scopeSearch} className={({ isActive }) => isActive ? 'is-active' : ''}>{item.title}</NavLink>)}</div>}
      </div>)}</nav>
      <div className="sp-sidebar-bottom">
        {access?.roles.some(g => g.role === 'system_admin') && <NavLink to={'/settings' + scopeSearch} className={({ isActive }) => `sp-nav-item ${isActive ? 'is-active' : ''}`}><Settings size={18} />系统设置</NavLink>}
        <div className="sp-user"><UserRound size={20} /><ScopedLink to="/account" title={currentUser} className="sp-account-link"><strong>{currentUser}</strong><small>账号设置</small></ScopedLink><Button onClick={() => onNavigate(Screen.LOGIN_SELECT)} className="sp-icon-button" title="退出登录" aria-label="退出登录"><LogOut size={17} /></Button></div>
      </div>
    </aside>
    {menuOpen && <button className="sp-menu-overlay" aria-label="关闭导航" onClick={() => { setMenuOpen(false); menuRef.current?.focus(); }} />}
    <div className="sp-frame">
      <header className={`sp-topbar ${module?.path === '/assistant' || isModuleHome ? 'sp-topbar-assistant' : ''}`}>
        <button ref={menuRef} className="sp-button sp-icon-button sp-menu-toggle" onClick={() => setMenuOpen(!menuOpen)} aria-label={menuOpen ? '收起导航' : '打开导航'} aria-expanded={menuOpen}>{menuOpen ? <X size={20} /> : <Menu size={20} />}</button>
        {module?.path === '/assistant' ? <div className="sp-topbar-heading"><h1>智能助手</h1><p>保留问题、分析范围和调查依据，协作跟进上海揽收风险。</p></div> : isModuleHome ? <div ref={setHeadingTarget} className="sp-topbar-heading" /> : <nav className="sp-breadcrumb" aria-label="面包屑"><ScopedLink to="/assistant">运营空间</ScopedLink><ChevronRight size={14} />{module && location.pathname !== module.path ? <>{module.path === '/data' ? <span>{module.title}</span> : <ScopedLink to={module.path}>{module.title}</ScopedLink>}<ChevronRight size={14} /><span>{subPage?.title || '详情'}</span></> : <span>{module?.title || (location.pathname === '/settings' ? '系统设置' : location.pathname === '/account' ? '账号设置' : '页面未找到')}</span>}</nav>}
        <div className="sp-topbar-tools"><select className="lg-region-selector" aria-label="运营区域" value={region} disabled={!config?.region_ids.length || status === 'creating' || status === 'loading'} onChange={event => setRegion(event.target.value)}>
          {!config ? <option value="">上海物流运营空间 · 加载中</option> : !config.region_ids.length ? <option value="">上海物流运营空间 · 暂无授权区域</option> : <><option value="all">上海物流运营空间 · 全部授权区域</option>{config.region_ids.map(value => <option key={value} value={value}>上海物流运营空间 · 区域 {value}</option>)}</>}
        </select><StatusTag tone="info">历史数据回放</StatusTag>
          <div className="sp-notification"><button ref={notificationRef} className="sp-button sp-icon-button" title="通知" aria-label="通知" aria-expanded={notificationsOpen} onClick={() => setNotificationsOpen(!notificationsOpen)}><Bell size={18} /></button>{notificationsOpen && <div className="sp-notification-panel"><strong>通知</strong><p>尚未接入通知服务</p></div>}</div><AccountMenu />
        </div>
      </header>
      <main id="supply-content" className={`sp-content ${module?.path === '/assistant' ? 'sp-assistant-content' : ''}`} tabIndex={-1}>

        {parentPath && parentPath !== module?.path && location.pathname !== '/workspace' && <ScopedLink to={parentPath} className="sp-parent-link">返回上级</ScopedLink>}
        <Outlet key={`${access?.account}:${access?.version}`} context={{ factory, currentUser: access?.account || currentUser, headingTarget: isModuleHome ? headingTarget : null } satisfies SupplyContext} />
      </main>
    </div>
  </div>;
}
