import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { UserRound } from 'lucide-react';
import { request } from './api';
import { useAccess } from './LogisticsShell';

type Options = {enabled:boolean; accounts:Array<{account:string;name:string;roles:string[]}>};
export default function AccountMenu() {
  const {access}=useAccess();
  const [open,setOpen]=useState(false),[options,setOptions]=useState<Options|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const panel=useRef<HTMLDivElement>(null), trigger=useRef<HTMLButtonElement>(null);
  useEffect(()=>{
    if(!open)return;
    const controller=new AbortController();
    request<Options>('/access/test-accounts',undefined,controller.signal).then(setOptions).catch(e=>{if(!controller.signal.aborted)setError(e.message);});
    function close(e:PointerEvent){if(!panel.current?.contains(e.target as Node))setOpen(false);}
    function escape(e:KeyboardEvent){if(e.key==='Escape'){setOpen(false);trigger.current?.focus();}}
    document.addEventListener('pointerdown',close);document.addEventListener('keydown',escape);
    return()=>{controller.abort();document.removeEventListener('pointerdown',close);document.removeEventListener('keydown',escape);};
  },[open]);
  async function change(account?:string){
    if(document.querySelector('[data-unsaved="true"]')&&!window.confirm('当前有未提交内容。聊天草稿保留在原账号，其他未保存编辑可能丢失。继续切换或退出？'))return;
    setBusy(true);setError('');
    try{
      if(account)await request('/access/switch-test-account',{account});
      else {
        const base=(import.meta.env.VITE_AUTH_API_BASE_URL||'').replace(/\/$/,'');
        const response=await fetch(`${base}/api/auth/logout`,{method:'POST',credentials:'include'});
        if(!response.ok)throw new Error('退出失败，请重试。');
      }
      // 整页导航终止旧请求、销毁内存缓存，并重新读取真实会话和授权。
      window.location.replace(account?'/workspace':'/login');
    }catch(e){setError((e as Error).message);setBusy(false);}
  }
  return <div ref={panel} className="sp-account-menu">
    <button ref={trigger} className="sp-button sp-icon-button" aria-label="账号菜单" aria-expanded={open} aria-controls="account-panel" onClick={()=>setOpen(!open)}><UserRound size={18}/></button>
    {open&&<section id="account-panel" className="sp-account-dropdown" aria-label="账号切换">
      <strong>{access?.account}</strong><p>{access?.roles.map(g=>access.roleCatalog[g.role]).join('、')||'待授权'}</p>
      {options?.enabled&&<><small>本地测试账号</small>{options.accounts.map(a=><button className="sp-button" key={a.account} disabled={busy||a.account===access?.account} onClick={()=>void change(a.account)}><span>{a.account===access?.account?'当前：':'切换到'}{a.name}<small>{a.roles.join('、')||'待授权'}</small></span></button>)}<button className="sp-button" disabled={busy} onClick={()=>void change()}>管理员登录（需密码）</button></>}
      <Link to="/account" onClick={()=>setOpen(false)}>账号设置</Link><button className="sp-button" disabled={busy} onClick={()=>void change()}>退出登录</button>
      {busy&&<p role="status">正在切换账号…</p>}{error&&<p role="alert">{error}</p>}
    </section>}
  </div>;
}
