/// <reference types="vite/client" />
import { useEffect, useState, type FormEvent } from 'react';
import { Eye, EyeOff } from 'lucide-react';
import { Screen } from '../types';
import AuthLayout from './AuthLayout';

export default function RegisterScreen({ onNavigate, embedded = false }: { onNavigate: (screen: Screen) => void; embedded?: boolean }) {
  const [accountValue, setAccountValue] = useState('');
  const [codeValue, setCodeValue] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [showConfirmPassword, setShowConfirmPassword] = useState(false);
  const [countdown, setCountdown] = useState(0);
  const [sendingCode, setSendingCode] = useState(false);
  const [loading, setLoading] = useState(false);
  const [feedback, setFeedback] = useState('');
  const [succeeded, setSucceeded] = useState(false);
  const authBaseUrl = (import.meta.env.VITE_AUTH_API_BASE_URL || '').replace(/\/$/, '');

  useEffect(() => {
    if (countdown === 0) return;
    const timer = window.setTimeout(() => setCountdown(countdown - 1), 1000);
    return () => window.clearTimeout(timer);
  }, [countdown]);

  const startCountdown = async () => {
    if (countdown || sendingCode || !accountValue.trim()) return;
    const email = document.getElementById('register-email') as HTMLInputElement;
    if (!email.reportValidity()) return;
    setSendingCode(true); setFeedback(''); setSucceeded(false);
    try {
      const response = await fetch(`${authBaseUrl}/api/auth/verification-codes`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'include',
        body: JSON.stringify({ account: accountValue.trim(), type: 'email', purpose: 'register' }),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.message || '验证码发送失败，请稍后重试。');
      setCountdown(60); setSucceeded(true); setFeedback('验证码已发送。');
    } catch (error) {
      setFeedback(error instanceof TypeError ? '无法连接认证服务，请稍后重试。' : error instanceof Error ? error.message : '验证码发送失败。');
    } finally { setSendingCode(false); }
  };

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault(); setFeedback(''); setSucceeded(false);
    if (!/^[0-9]{6}$/.test(codeValue)) { setFeedback('请输入 6 位数字验证码。'); return; }
    if (password !== confirmPassword) { setFeedback('两次输入的密码不匹配。'); return; }
    if (password.length < 8) { setFeedback('密码至少需要 8 位。'); return; }
    setLoading(true);
    try {
      const response = await fetch(`${authBaseUrl}/api/auth/register`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'include',
        body: JSON.stringify({ account: accountValue.trim(), type: 'email', code: codeValue, password }),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.message || '注册失败，请稍后重试。');
      setSucceeded(true); setFeedback('注册成功，请直接登录。');
    } catch (error) {
      setFeedback(error instanceof TypeError ? '无法连接认证服务，请稍后重试。' : error instanceof Error ? error.message : '注册失败。');
    } finally { setLoading(false); }
  };

  const panel = <section className="scope-panel">
      <button className="scope-auth-link scope-back" onClick={() => onNavigate(Screen.LOGIN)}>← 返回登录</button>
      <h2>创建你的账号</h2><p className="scope-panel-sub">使用邮箱注册 Scope，开始协作。</p>
      <form onSubmit={handleSubmit}>
        <div className="scope-field"><label htmlFor="register-email">邮箱地址</label><input id="register-email" type="email" required autoComplete="email" value={accountValue} onChange={e => setAccountValue(e.target.value)} placeholder="请输入邮箱地址" /></div>
        <div className="scope-field"><label htmlFor="register-code">邮箱验证码</label><div className="scope-code"><input id="register-code" type="text" inputMode="numeric" pattern="[0-9]{6}" required value={codeValue} onChange={e => setCodeValue(e.target.value.replace(/\D/g, '').slice(0, 6))} placeholder="6 位验证码" /><button type="button" disabled={countdown > 0 || sendingCode || !accountValue.trim()} onClick={startCountdown}>{sendingCode ? '发送中...' : countdown > 0 ? `${countdown}s 后重发` : '发送验证码'}</button></div></div>
        <div className="scope-field"><label htmlFor="register-password">设置密码</label><div className="scope-password"><input id="register-password" type={showPassword ? 'text' : 'password'} minLength={8} required autoComplete="new-password" value={password} onChange={e => setPassword(e.target.value)} placeholder="至少 8 位字符" /><button type="button" aria-label={showPassword ? '隐藏密码' : '显示密码'} title={showPassword ? '隐藏密码' : '显示密码'} onClick={() => setShowPassword(!showPassword)}>{showPassword ? <EyeOff size={18} /> : <Eye size={18} />}</button></div></div>
        <div className="scope-field"><label htmlFor="register-confirm">确认密码</label><div className="scope-password"><input id="register-confirm" type={showConfirmPassword ? 'text' : 'password'} required autoComplete="new-password" value={confirmPassword} onChange={e => setConfirmPassword(e.target.value)} placeholder="再次输入密码" /><button type="button" aria-label={showConfirmPassword ? '隐藏确认密码' : '显示确认密码'} title={showConfirmPassword ? '隐藏确认密码' : '显示确认密码'} onClick={() => setShowConfirmPassword(!showConfirmPassword)}>{showConfirmPassword ? <EyeOff size={18} /> : <Eye size={18} />}</button></div></div>
        <label className="scope-agreement"><input type="checkbox" required />我已阅读并同意用户协议与隐私政策</label>
        <button className="scope-primary" type="submit" disabled={loading}>{loading ? '正在注册...' : '创建账号'}</button>
        {feedback && <p className={`scope-feedback ${succeeded ? 'scope-feedback-success' : ''}`} role="status">{feedback}</p>}
      </form>
      <p className="scope-center">已有账号？ <button className="scope-auth-link" onClick={() => onNavigate(Screen.LOGIN)}>直接登录</button></p>
    </section>;

  return embedded ? panel : <AuthLayout onNavigate={onNavigate} withIntro switchText="已有账号？" switchLabel="登录" switchTarget={Screen.LOGIN}>{panel}</AuthLayout>;
}
