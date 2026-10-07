const base = (import.meta.env.VITE_AUTH_API_BASE_URL || '').replace(/\/$/, '');
export const dataFileUrl = (batchId: string) => `${base}/api/data/imports/${encodeURIComponent(batchId)}/file${sessionStorage.getItem('business-workspace') ? `?workspaceId=${encodeURIComponent(sessionStorage.getItem('business-workspace')!)}` : ''}`;

export async function dataRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    const headers = new Headers(options.headers);
    const workspace = sessionStorage.getItem('business-workspace');
    if (workspace) headers.set('X-Workspace-Id', workspace);
    response = await fetch(`${base}/api/data${path}`, { credentials: 'include', ...options, headers });
  } catch { throw new Error('无法连接数据服务，请稍后重试。'); }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(response.status === 401 ? '登录已失效，请重新登录。' : body.message || `请求失败（${response.status}）。`);
  }
  return response.json();
}

export function jsonRequest<T>(path: string, body: unknown): Promise<T> {
  return dataRequest<T>(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
}

export type Preview = { draftId: string; filename: string; domain: string; factory: string; columns: string[]; sample: Record<string, string>[]; total: number; sheets: string[]; sheet: string };
export type CheckedRow = { row: number; material: string; quantity: string; unit: string; normalizedQuantity: string | null; status: string };
export type Issue = { domain?: string; id?: string; row?: number; row_number?: number; severity: string; rule: string; status?: string; originalValue?: string; original_value?: string; reason: string; impact: string; material?: string; quantity?: string; unit?: string; normalized_quantity?: string; batch_id?: string; factory?: string; filename?: string; assignee?: string; note?: string };
export type Check = { rows: CheckedRow[]; issues: Issue[]; blocking: number; isolated: number };
export type Batch = { id: string; filename: string; domain: string; factory: string; owner: string; sheet: string; total: number; imported: number; isolated: number; created_at: string; records?: Array<{ row_number: number; material: string; quantity: string; unit: string; normalized_quantity: string | null; status: string }>; issues?: Issue[] };
export type Rule = { id: number; material: string; factory: string; from_unit: string; to_unit: string; factor: string; evidence: string; confirmed_by: string; valid_from: string; version: number };
export type MaterialRule = { id: number; source_code: string; standard_code: string; factory: string; evidence: string; confirmed_by: string; valid_from: string; version: number };
export type Change = { id: number; batch_id: string; issue_id: string | null; kind: string; details: string; filename: string; factory: string; created_at: string };
