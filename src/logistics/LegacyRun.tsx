import { useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { request } from './api';

export default function LegacyRun() {
  const { runId = '' } = useParams(); const navigate = useNavigate();
  const [associate, setAssociate] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController(); setError(''); setAssociate(false);
    request<{ path?: string; requires_association?: boolean }>(`/runs/${encodeURIComponent(runId)}/destination`, undefined, controller.signal)
      .then(value => { if (!controller.signal.aborted) { if (value.path) navigate(value.path, { replace: true }); else setAssociate(!!value.requires_association); } })
      .catch(cause => { if (!controller.signal.aborted) setError(cause.message); });
    return () => controller.abort();
  }, [runId, navigate]);
  async function open() {
    setBusy(true); setError('');
    try { const value = await request<{ path: string }>(`/conversations/for-run/${encodeURIComponent(runId)}`, {}); navigate(value.path, { replace: true }); }
    catch (cause) { setError((cause as Error).message); }
    finally { setBusy(false); }
  }
  return <section className="sp-section"><h1>打开已有调查</h1>{error ? <p role="alert">{error}</p> : associate ? <><p>原调查仍在运行或需要处理，可关联到自己的会话继续访问。此操作不会重新执行调查。</p><button className="sp-button" disabled={busy} onClick={() => void open()}>关联原调查并打开会话</button></> : <p role="status">正在核对访问权限及业务入口…</p>}<Link to="/assistant">返回智能助手</Link></section>;
}
