/// <reference types="vite/client" />
import { startTransition, useEffect, useState } from 'react';
import { Navigate, Outlet, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { Screen } from './types';
import LoginSelectScreen from './components/LoginSelectScreen';
import ForgotPasswordScreen from './components/ForgotPasswordScreen';
import AccountSettingsScreen from './components/AccountSettingsScreen';
import RegisterScreen from './components/RegisterScreen';
import LoginScreen from './components/LoginScreen';
import LogisticsShell from './logistics/LogisticsShell';
import LogisticsPages from './logistics/LogisticsPages';

const screenPaths: Partial<Record<Screen, string>> = {
  [Screen.WORKSPACE]: '/assistant',
  [Screen.ACCOUNT_SETTINGS]: '/account',
  [Screen.LOGIN_SELECT]: '/login-select',
  [Screen.LOGIN]: '/login',
  [Screen.REGISTER]: '/register',
  [Screen.FORGOT_PASSWORD]: '/forgot-password',
};

export default function App() {
  const [currentUser, setCurrentUser] = useState<string | null>(null);
  const [sessionError, setSessionError] = useState('');
  const navigate = useNavigate();
  const location = useLocation();
  const authBaseUrl = (import.meta.env.VITE_AUTH_API_BASE_URL || '').replace(/\/$/, '');

  async function readSession() {
    const response = await fetch(`${authBaseUrl}/api/auth/session`, { credentials: 'include' });
    if (response.status === 401) return '';
    if (!response.ok) throw new Error('认证服务暂不可用，请重试。');
    const result = await response.json();
    return result.user?.name || '';
  }

  useEffect(() => {
    let active = true;
    readSession().then(user => { if (active) setCurrentUser(user); })
      .catch(error => { if (active) setSessionError(error.message || '无法连接认证服务。'); });
    return () => { active = false; };
  }, [authBaseUrl]);

  const handleNavigate = async (screen: Screen) => {
    if (screen === Screen.LOGIN_SELECT && currentUser) {
      try {
        const response = await fetch(`${authBaseUrl}/api/auth/logout`, { method: 'POST', credentials: 'include' });
        if (!response.ok) throw new Error();
        startTransition(() => {
          setCurrentUser('');
          navigate('/login-select', { replace: true });
        });
      } catch { alert('退出登录失败，请稍后重试。'); }
      return;
    }
    const target = screenPaths[screen] || '/assistant';
    if (!['/login-select', '/login', '/register', '/forgot-password'].includes(target)) {
      try {
        const user = await readSession();
        setCurrentUser(user);
        if (!user) { navigate(`/login?next=${encodeURIComponent(target)}`); return; }
      } catch { alert('无法确认登录状态，请稍后重试。'); return; }
    }
    const next = new URLSearchParams(location.search).get('next');
    const afterLogin = next && /^\/(?!\/)/.test(next) && !/^\/(login|register|forgot-password)/.test(next) ? next : '/assistant';
    navigate(screen === Screen.WORKSPACE && location.pathname === '/login' ? afterLogin : target);
  };

  if (currentUser === null) return <div className="session-state" role="status">
    {sessionError ? <><p>{sessionError}</p><button className="sp-button" onClick={() => window.location.reload()}>重新连接</button></> : '正在检查登录状态...'}
  </div>;

  const protectedContent = currentUser ? <Outlet /> : <Navigate to={`/login?next=${encodeURIComponent(location.pathname + location.search)}`} replace />;
  const authNavigate = (screen: Screen) => {
    if ([Screen.LOGIN, Screen.REGISTER, Screen.FORGOT_PASSWORD].includes(screen)) {
      navigate((screenPaths[screen] || '/login') + location.search);
    } else { void handleNavigate(screen); }
  };

  return <Routes>
    <Route path="/" element={<Navigate to={currentUser ? '/assistant' : '/login-select'} replace />} />
    <Route path="/login-select" element={<LoginSelectScreen onNavigate={authNavigate} />} />
    <Route path="/login" element={<LoginScreen onNavigate={authNavigate} />} />
    <Route path="/register" element={<RegisterScreen onNavigate={authNavigate} />} />
    <Route path="/forgot-password" element={<ForgotPasswordScreen onNavigate={authNavigate} />} />
    <Route element={protectedContent}>
      <Route element={<LogisticsShell currentUser={currentUser} onNavigate={handleNavigate} />}>
        <Route path="/account" element={<AccountSettingsScreen currentUser={currentUser} />} />
        {['workspace', 'metrics', 'assistant', 'assistant/:threadId', 'risks', 'risks/:eventId', 'approvals', 'approvals/:approvalId', 'knowledge', 'knowledge/:knowledgeId', 'data', 'data/connections', 'data/catalog', 'data/governance', 'data/preview/pickup_tasks', 'runs', 'runs/:runId', 'settings'].map(path => <Route key={path} path={path} element={<LogisticsPages />} />)}
        <Route path="*" element={<LogisticsPages notFound />} />
      </Route>
    </Route>
  </Routes>;
}
