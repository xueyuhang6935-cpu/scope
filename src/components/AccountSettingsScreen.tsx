import { BellRing, Camera, KeyRound, MessageSquare, ShieldCheck, UserRound } from 'lucide-react';
import type { ButtonHTMLAttributes, ReactNode } from 'react';
import { Link as ScopedLink } from 'react-router-dom';

function Button(props: ButtonHTMLAttributes<HTMLButtonElement>) { return <button type="button" className="sp-button" {...props} />; }
function PageHeading({ title }: { title: string }) { return <div className="sp-heading"><h1>{title}</h1></div>; }
function StatusTag({ children }: { tone: string; children: ReactNode }) { return <span className="sp-status sp-status-warning">{children}</span>; }

export default function AccountSettingsScreen({ currentUser }: { currentUser: string }) {
  return <>
    <PageHeading title="账号设置" />
    <section className="sp-account-profile" aria-label="账号信息">
      <div className="sp-account-avatar"><UserRound size={32} /></div>
      <div><h2>{currentUser}</h2><p>Scope</p></div>
      <Button disabled title="个人资料服务尚未接入"><Camera size={16} />编辑个人资料</Button>
    </section>
    <div className="sp-columns">
      <section className="sp-section"><h2 className="sp-icon-heading"><ShieldCheck size={18} />安全设置</h2>
        <div className="sp-account-row"><label htmlFor="account-two-factor">双重认证 (2FA)<small>认证状态尚不可查询</small></label><input id="account-two-factor" type="checkbox" role="switch" disabled /></div>
        <div className="sp-account-row"><span>管理密码<small>上次修改时间尚不可查询</small></span><Button disabled title="账号修改密码服务尚未接入"><KeyRound size={15} />修改密码</Button></div>
        <div className="sp-account-row"><span>第三方登录活动</span><Button disabled title="登录活动服务尚未接入">查看历史</Button></div>
      </section>
      <section className="sp-section"><h2 className="sp-icon-heading"><BellRing size={18} />信息提示</h2>
        <label className="sp-account-row" htmlFor="account-email-updates"><span>电子邮件版本更新信息<small>设置状态尚不可查询</small></span><input id="account-email-updates" type="checkbox" disabled /></label>
        <label className="sp-account-row" htmlFor="account-task-alerts"><span>任务完成通知<small>设置状态尚不可查询</small></span><input id="account-task-alerts" type="checkbox" disabled /></label>
        <p><StatusTag tone="warning">账号设置服务尚未接入</StatusTag></p>
      </section>
    </div>
    <section className="sp-section"><h2 className="sp-icon-heading"><MessageSquare size={18} />微信绑定</h2><p>绑定信息尚不可查询。</p><Button disabled title="微信绑定服务尚未接入"><MessageSquare size={16} />绑定微信</Button></section>
    <section className="sp-section"><h2>账户用量与订阅</h2><dl className="sp-facts"><div><dt>剩余 Token</dt><dd>尚不可查询</dd></div><div><dt>总计消耗</dt><dd>尚不可查询</dd></div><div><dt>账户余额</dt><dd>尚不可查询</dd></div><div><dt>订阅计划</dt><dd>尚不可查询</dd></div></dl></section>
    <ScopedLink to="/workspace">返回工作台</ScopedLink>
  </>;
}
