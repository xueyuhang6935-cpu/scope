export type Grant = { role: string; city: string; regions: string[] };
export type Member = { account: string; enabled: number; version: number; roles: Grant[] };
export type Access = Member & { workspaceId: string; workspaceName: string; roleCatalog: Record<string, string>; regionCatalog: string[] };
export type ChatTask = { order_id: string; evidence_id: string; risk_state?: string; current_wait_minutes?: number; current_overdue_minutes?: number };
export type ChatDraft = { draft_version: number; draft_hash: string; status: string; change_note: string;
  content: { title: string; note: string; strategy: string; tasks: ChatTask[]; actions: { order_id: string; type: string; sequence: number }[]; limitations: string[] } };
export type ChatReport = { report_id: string; scope: { business_as_of: string; region_ids: string[] };
  summary: { metrics: Record<string, { name: string; value: number | null; status: string }> }; focus_tasks: ChatTask[]; limitations: string[] };
export type ChatConversation = { conversation_id: string; title: string; version: number; restricted: boolean;
  updated_at?: string; status?: string;
  context: { region_ids?: string[]; business_as_of?: string; run_id?: string; task_ids?: string[]; mode?: string; draft_version?: number; knowledge_ids?: string[] } };
export type ModelReport = { source?: string; value: { status?: string; claims?: { statement: string; kind: string; limitation?: string; evidence_ids: string[] }[];
  limitations?: string[]; unresolved_questions?: string[]; next_steps?: string[]; evidence_ids?: string[] } };
export type KnowledgeEntry = { knowledge_id: string; run_id: string; title: string; result_kind: string; result_status: string;
  archived_at: string; abstract: string; scope: { region_ids: string[]; business_as_of: string; snapshot_id: string; batch_id: string; city: string }; limitations: string[] };
export type RunView = { run_id: string; status: string; current_node: string; mode?: string; question?: string; error_code?: string;
  business_as_of?: string; region_ids?: string[]; can_cancel?: boolean; can_report?: boolean;
  result?: { stop_reason?: string; report?: ModelReport; evidence_ids?: string[]; unfinished_steps?: { step_id: string; question: string }[] } };
export type ChatMessage = { message_id: string; seq: number; role: string; text: string; status: string;
  result: { run_id?: string; evidence_ids?: string[]; report?: ChatReport; draft?: ChatDraft;
    partial_report?: { value?: { limitations?: string[]; unresolved_questions?: string[] } }; source?: string;
    knowledge_reference?: KnowledgeEntry;
    proposal?: { proposal_version: number; status: string } } | null };
export async function request<T>(path: string, body?: unknown, signal?: AbortSignal, timeout = 30000): Promise<T> {
  const base = (import.meta.env.VITE_AUTH_API_BASE_URL || '').replace(/\/$/, '');
  const response = await fetch(`${base}/api/logistics${path}`, { credentials: 'include',
    signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(timeout)]) : AbortSignal.timeout(timeout),
    ...(body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }) });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw Object.assign(new Error(data.message || (response.status === 401 ? '登录已失效，请重新登录。' : `请求失败（${response.status}）`)), { status: response.status });
  }
  return response.json();
}
