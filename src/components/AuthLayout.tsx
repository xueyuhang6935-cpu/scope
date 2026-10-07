import type { ReactNode } from 'react';
import { Screen } from '../types';
import './auth.css';

export default function AuthLayout({ children, onNavigate, headerText, switchText, switchLabel, switchTarget, withIntro = false, networkChoice = false }: {
  children: ReactNode;
  onNavigate: (screen: Screen) => void;
  headerText?: string;
  switchText?: string;
  switchLabel?: string;
  switchTarget?: Screen;
  withIntro?: boolean;
  networkChoice?: boolean;
}) {
  return <div className={`scope-auth${networkChoice ? ' scope-auth-network' : ''}`}>
    <header className="scope-auth-header">
      <button className="scope-brand" onClick={() => onNavigate(Screen.LOGIN_SELECT)} aria-label="返回 Scope 登录">
        <span className="scope-brand-mark">S</span><strong>Scope</strong><span className="scope-brand-subtitle">供应链智能助手</span>
      </button>
      {!networkChoice && <div className="scope-header-switch">{headerText || <>{switchText} <button onClick={() => switchTarget && onNavigate(switchTarget)}>{switchLabel} →</button></>}</div>}
    </header>
    {withIntro ? <main className="scope-auth-shell">
      <section className="scope-auth-intro" aria-label="Scope 产品介绍">
        <h1>看清风险，<br /><em>让每一步决策有依据。</em></h1>
      </section>
      {children}
    </main> : children}
    <footer className="scope-auth-footer">Scope · 让供应链协作更有依据</footer>
  </div>;
}
