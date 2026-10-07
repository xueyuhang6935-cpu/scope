export type Plan = { intent: string; factory: string; material: string; supplier: string; period: string };
export type ChatAction = 'query' | 'explain' | 'refresh' | 'investigate' | 'simulate' | 'approval_request' | 'unsupported_execution';
export type RiskOption = { factory: string; material: string; revision: string; shortage: string };
export type AnalysisResult = { answer?: string; evidenceCapturedAt?: string; evidenceSource?: { scenarioId: string; asOf: string; days: number }; refreshNotice?: string; workflow?: { planId?: string; riskId: string; runId: string; basedOnTaskId: string; windowDays?: number }; basedOnTaskId?: string | null; summary: string; scope: Plan; capturedAt: string; ruleVersion: string; records: Array<{ record_id: number; batch_id: string; material: string; factory: string; filename: string; normalized_quantity?: string; normalized_unit?: string; reason?: string; impact?: string; rule?: string }>; limitations: string[]; missing: string[]; recommendations: string[]; truncated: boolean; calculation?: import('./RiskWorkspace').Calculation };
export type Task = { id: string; thread_id: string; text: string; status: string; plan: Plan; result: AnalysisResult | null; error: string | null; updated_at: string; source?: { scenarioId: string; asOf: string; days: number }; planning?: { action?: ChatAction; riskOptions?: RiskOption[]; notice?: string; tokens?: number | null; questions?: string[]; execution?: string } };
export type Thread = { id: string; title: string; factory: string; created_at: string; tasks?: Task[] };
export type Capabilities = { available: boolean; intents: Record<string, string>; parser: string; limitations: string[] };
const base = (import.meta.env.VITE_AUTH_API_BASE_URL || '').replace(/\/$/, '');
export async function supplyRequest<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${base}/api/supply${path}`, { credentials: 'include', signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(20000)]) : AbortSignal.timeout(20000),
    headers: { ...(sessionStorage.getItem('business-workspace') ? { 'X-Workspace-Id': sessionStorage.getItem('business-workspace')! } : {}), ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
    ...(body === undefined ? {} : { method: 'POST', body: JSON.stringify(body) }) });
  if (!response.ok) {
    const value = await response.json().catch(() => ({}));
    throw Object.assign(new Error(response.status === 401 ? '登录已失效，请重新登录。' : value.message || `分析服务请求失败（${response.status}）。`), { status: response.status });
  }
  return response.json();
}
export const taskLabels: Record<string, string> = { planning: '解析问题中', awaiting_confirmation: '等待澄清', queued: '等待分析', running: '分析中', completed: '完成', insufficient_data: '数据不足', no_data: '无数据', failed: '失败', cancelled: '已取消' };
export const activeTask = (task: Task) => ['planning', 'awaiting_confirmation', 'queued', 'running'].includes(task.status);
