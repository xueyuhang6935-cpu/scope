/// <reference types="vite/client" />
/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import { useEffect, useState, FormEvent } from 'react';
import { motion } from 'motion/react';
import { Phone, CheckCircle, ShieldCheck, Lock, RefreshCw, ArrowLeft } from 'lucide-react';
import { Screen } from '../types';

interface ForgotPasswordScreenProps {
  onNavigate: (screen: Screen) => void;
}

export default function ForgotPasswordScreen({ onNavigate }: ForgotPasswordScreenProps) {
  const [phone, setPhone] = useState('');
  const [code, setCode] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  
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
    if (countdown || sendingCode || !phone) return;
    setSendingCode(true);
    setFeedback('');
    setSucceeded(false);
    try {
      const response = await fetch(`${authBaseUrl}/api/auth/verification-codes`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ account: phone.trim(), type: 'phone', purpose: 'reset-password' }),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.message || '验证码发送失败，请稍后重试。');
      setCountdown(60);
      setSucceeded(true);
      setFeedback('验证码已发送。');
    } catch (error) {
      setFeedback(error instanceof TypeError ? '无法连接认证服务，请稍后重试。' : error instanceof Error ? error.message : '验证码发送失败。');
    } finally {
      setSendingCode(false);
    }
  };

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setFeedback('');
    setSucceeded(false);
    if (password !== confirmPassword) {
      setFeedback('两次输入的密码不一致。');
      return;
    }
    if (password.length < 8) {
      setFeedback('密码至少需要 8 位。');
      return;
    }
    setLoading(true);
    try {
      const response = await fetch(`${authBaseUrl}/api/auth/reset-password`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ phone: phone.trim(), code: code.trim(), password }),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.message || '密码重置失败，请稍后重试。');
      setSucceeded(true);
      setFeedback('密码已重置，请返回登录。');
    } catch (error) {
      setFeedback(error instanceof TypeError ? '无法连接认证服务，请稍后重试。' : error instanceof Error ? error.message : '密码重置失败。');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="relative min-h-screen bg-[#f6fafe] overflow-hidden flex flex-col justify-center items-center p-6 bg-grid">
      <main className="w-full max-w-[440px] z-10">
        
        {/* Brand identity */}
        <div className="text-center mb-10">
          <h1 className="text-3xl font-extrabold text-[#0F172A] tracking-tight">OPENHORSE AI</h1>
          <p className="text-xs font-bold text-slate-400 uppercase tracking-[0.2em] mt-2">Intelligence Power</p>
        </div>

        {/* Form panel card */}
        <motion.section 
          className="bg-white auth-card rounded-2xl p-8 hover:shadow-xl transition-all duration-300 border border-slate-200"
          initial={{ opacity: 0, y: 15 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.4 }}
        >
          <header className="mb-6 text-center">
            <h2 className="text-2xl font-bold text-[#0F172A]">找回密码</h2>
            <p className="text-sm text-slate-500 mt-2">重置您的账户访问权限</p>
          </header>

          <form onSubmit={handleSubmit} className="space-y-5" id="forgot-password-form">
            {/* Phone Number */}
            <div className="space-y-1.5">
              <label className="text-xs font-semibold uppercase tracking-wider text-slate-500 block">手机号</label>
              <div className="relative">
                <span className="absolute left-3.5 top-1/2 -translate-y-1/2 text-slate-400">
                  <Phone className="w-5 h-5" />
                </span>
                <input 
                  type="tel"
                  required
                  value={phone}
                  onChange={(e) => setPhone(e.target.value)}
                  className="w-full bg-[#f0f4f8] border border-slate-200 focus:border-[#0F172A] focus:ring-1 focus:ring-[#0F172A] rounded-xl py-3 pl-11 pr-4 text-sm text-[#0F172A] transition-all outline-none"
                  placeholder="请输入您的手机号"
                />
              </div>
            </div>

            {/* Verification Code */}
            <div className="space-y-1.5">
              <label className="text-xs font-semibold uppercase tracking-wider text-slate-500 block">验证码</label>
              <div className="flex gap-3">
                <div className="relative flex-1">
                  <span className="absolute left-3.5 top-1/2 -translate-y-1/2 text-slate-400">
                    <ShieldCheck className="w-5 h-5" />
                  </span>
                  <input 
                    type="text"
                    required
                    value={code}
                    onChange={(e) => setCode(e.target.value)}
                    className="w-full bg-[#f0f4f8] border border-slate-200 focus:border-[#0F172A] focus:ring-1 focus:ring-[#0F172A] rounded-xl py-3 pl-11 pr-4 text-sm text-[#0F172A] transition-all outline-none"
                    placeholder="输入验证码"
                  />
                </div>
                <button 
                  type="button"
                  id="send-code-btn"
                  onClick={startCountdown}
                  disabled={countdown > 0 || sendingCode || !phone}
                  className={`px-4 py-3 text-xs font-bold rounded-xl whitespace-nowrap min-w-[120px] active:scale-95 transition-all text-center border cursor-pointer border-slate-200 ${
                    countdown > 0 || sendingCode
                      ? 'bg-slate-100 text-slate-400 cursor-not-allowed' 
                      : 'bg-[#0F172A] text-white hover:bg-slate-800'
                  }`}
                >
                  {sendingCode ? '发送中...' : countdown > 0 ? `${countdown}s 后重发` : '发送验证码'}
                </button>
              </div>
            </div>

            {/* New Password */}
            <div className="space-y-1.5">
              <label className="text-xs font-semibold uppercase tracking-wider text-slate-500 block">新密码</label>
              <div className="relative">
                <span className="absolute left-3.5 top-1/2 -translate-y-1/2 text-slate-400">
                  <Lock className="w-5 h-5" />
                </span>
                <input 
                  type="password"
                  required
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="w-full bg-[#f0f4f8] border border-slate-200 focus:border-[#0F172A] focus:ring-1 focus:ring-[#0F172A] rounded-xl py-3 pl-11 pr-4 text-sm text-[#0F172A] transition-all outline-none"
                  placeholder="请输入新密码"
                />
              </div>
            </div>

            {/* Confirm Password */}
            <div className="space-y-1.5">
              <label className="text-xs font-semibold uppercase tracking-wider text-slate-500 block">确认密码</label>
              <div className="relative">
                <span className="absolute left-3.5 top-1/2 -translate-y-1/2 text-slate-400">
                  <CheckCircle className="w-5 h-5" />
                </span>
                <input 
                  type="password"
                  required
                  value={confirmPassword}
                  onChange={(e) => setConfirmPassword(e.target.value)}
                  className="w-full bg-[#f0f4f8] border border-slate-200 focus:border-[#0F172A] focus:ring-1 focus:ring-[#0F172A] rounded-xl py-3 pl-11 pr-4 text-sm text-[#0F172A] transition-all outline-none"
                  placeholder="再次确认新密码"
                />
              </div>
            </div>

            {feedback && <p role="status" className={`text-sm ${succeeded ? 'text-green-700' : 'text-red-600'}`}>{feedback}</p>}

            {/* Submit Button */}
            <button 
              type="submit"
              disabled={loading}
              className="w-full bg-[#F97316] text-white font-bold text-base py-3.5 rounded-xl hover:bg-orange-600 active:scale-98 hover:shadow cursor-pointer transition-all flex items-center justify-center gap-2"
            >
              {loading ? (
                <>
                  <RefreshCw className="animate-spin w-5 h-5" />
                  正在重置...
                </>
              ) : '确认重置'}
            </button>
          </form>

          {/* Footer Back link */}
          <footer className="mt-8 text-center">
            <a 
              href="#"
              onClick={(e) => {
                e.preventDefault();
                onNavigate(Screen.LOGIN);
              }}
              className="inline-flex items-center gap-1.5 font-bold text-sm text-slate-700 underline underline-offset-4 hover:text-[#0F172A] transition-colors cursor-pointer"
            >
              <ArrowLeft className="w-4 h-4" />
              返回登录
            </a>
          </footer>
        </motion.section>

        {/* Security watermark footer */}
        <div className="mt-12 flex justify-center items-center gap-4 text-on-surface-variant opacity-40">
          <div className="h-[1px] w-12 bg-slate-300"></div>
          <span className="text-xs text-slate-500 font-mono flex items-center gap-1">
             SECURE CHANNEL
          </span>
          <div className="h-[1px] w-12 bg-slate-300"></div>
        </div>
      </main>
    </div>
  );
}
