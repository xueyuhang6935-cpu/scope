/// <reference types="vite/client" />
import { useState, type FormEvent } from 'react';
import { Eye, EyeOff, LockKeyhole, MessageCircle, UserRound } from 'lucide-react';
import { Screen } from '../types';
import AuthLayout from './AuthLayout';

const rememberedAccountKey = 'scope.rememberedAccount';

export default function LoginScreen({ onNavigate, embedded = false }: { onNavigate: (screen: Screen) => void; embedded?: boolean }) {
  const [account, setAccount] = useState(() => localStorage.getItem(rememberedAccountKey) || '');
  const [rememberAccount, setRememberAccount] = useState(() => Boolean(localStorage.getItem(rememberedAccountKey)));
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [loading, setLoading] = useState(false);
  const [feedback, setFeedback] = useState('');
  const authBaseUrl = (import.meta.env.VITE_AUTH_API_BASE_URL || '').replace(/\/$/, '');

  const handleLogin = async (e: FormEvent) => {
    e.preventDefault();
    if (!account.trim() || !password) return;
    setLoading(true);
    setFeedback('');
    try {
      const response = await fetch(`${authBaseUrl}/api/auth/login`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'include',
        body: JSON.stringify({ account: account.trim(), password }),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.message || '登录失败，请检查账号和密码。');
      if (rememberAccount) localStorage.setItem(rememberedAccountKey, account.trim());
      else localStorage.removeItem(rememberedAccountKey);
      onNavigate(Screen.WORKSPACE);
    } catch (error) {
      setFeedback(error instanceof TypeError ? '无法连接认证服务，请稍后重试。' : error instanceof Error ? error.message : '登录失败。');
    } finally { setLoading(false); }
  };

  const handleWechatLogin = async () => {
    setFeedback('');
    try {
      const response = await fetch(`${authBaseUrl}/api/auth/wechat/authorize`, { credentials: 'include' });
      const result = await response.json().catch(() => ({}));
      if (response.ok && result.mock === true) {
        if (!window.confirm('当前为本地模拟微信登录，不会连接微信。是否以“本地微信测试用户”登录？')) return;
        const mockResponse = await fetch(`${authBaseUrl}/api/auth/wechat/mock`, { method: 'POST', credentials: 'include' });
        const mockResult = await mockResponse.json().catch(() => ({}));
        if (!mockResponse.ok) throw new Error(mockResult.message || '本地模拟登录失败。');
        onNavigate(Screen.WORKSPACE);
        return;
      }
      if (!response.ok || typeof result.authorizationUrl !== 'string') throw new Error(result.message || '微信登录启动失败。');
      window.location.assign(result.authorizationUrl);
    } catch (error) {
      setFeedback(error instanceof TypeError ? '无法连接认证服务，请稍后重试。' : error instanceof Error ? error.message : '微信登录启动失败。');
    }
  };

  const panel = <section className="scope-panel">
      <h2>欢迎回来</h2><p className="scope-panel-sub">登录 Scope，继续你的供应链任务。</p>
      <form onSubmit={handleLogin}>
        <div className="scope-field"><label htmlFor="login-account">账号</label><div className="scope-input-with-icon"><UserRound size={18} aria-hidden="true" /><input id="login-account" required autoComplete="username" value={account} onChange={e => setAccount(e.target.value)} placeholder="邮箱 / 手机号 / 登录名" /></div></div>
        <div className="scope-field">
          <label htmlFor="login-password">密码</label>
          <div className="scope-password scope-input-with-icon"><LockKeyhole size={18} aria-hidden="true" /><input id="login-password" type={showPassword ? 'text' : 'password'} required autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} placeholder="请输入登录密码" /><button type="button" aria-label={showPassword ? '隐藏密码' : '显示密码'} title={showPassword ? '隐藏密码' : '显示密码'} onClick={() => setShowPassword(!showPassword)}>{showPassword ? <EyeOff size={18} /> : <Eye size={18} />}</button></div>
        </div>
        <div className="scope-login-options"><label className="scope-agreement"><input type="checkbox" checked={rememberAccount} onChange={e => { setRememberAccount(e.target.checked); if (!e.target.checked) localStorage.removeItem(rememberedAccountKey); }} />记住登录账号</label><button type="button" className="scope-auth-link" onClick={() => onNavigate(Screen.FORGOT_PASSWORD)}>忘记密码？</button></div>
        <button className="scope-primary" type="submit" disabled={loading}>{loading ? '登录中...' : '登录'}</button>
        {feedback && <p className="scope-feedback" role="alert">{feedback}</p>}
      </form>
      {!embedded && <><div className="scope-divider">其他登录方式</div><button type="button" className="scope-secondary" onClick={handleWechatLogin}><MessageCircle size={17} color="#328666" />使用微信登录</button></>}
      <p className="scope-center">还没有账号？ <button className="scope-auth-link" onClick={() => onNavigate(Screen.REGISTER)}>注册账号</button></p>
    </section>;

  return embedded ? panel : <AuthLayout onNavigate={onNavigate} withIntro switchText="第一次使用？" switchLabel="注册" switchTarget={Screen.REGISTER}>{panel}</AuthLayout>;
}
