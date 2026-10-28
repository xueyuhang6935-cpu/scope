import { test, expect } from '@playwright/test';
import { spawn } from 'node:child_process';
import { createServer } from 'node:net';

test('Demo：真实后端反馈、跨账号驳回和批准、内部回执、新快照复核及重开', async ({ browser, request }, info) => {
  test.setTimeout(180000);
  const port = await new Promise<number>(resolve => {
    const server = createServer();
    server.listen(0, '127.0.0.1', () => {
      const address = server.address();
      const value = typeof address === 'object' && address ? address.port : 0;
      server.close(() => resolve(value));
    });
  });
  const code = `import sys,threading,uvicorn
from backend.test_logistics_events import EventsTests
t=EventsTests(); t.setUp()
server=uvicorn.Server(uvicorn.Config(t.app,host='127.0.0.1',port=int(sys.argv[1]),log_level='error'))
def stop():
    sys.stdin.readline()
    server.should_exit=True
threading.Thread(target=stop,daemon=True).start()
try: server.run()
finally: t.tearDown()
`;
  const child = spawn('.venv/Scripts/python.exe', ['-X', 'utf8', '-c', code, String(port)], { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
  let errors = ''; child.stderr.on('data', chunk => { errors += String(chunk); });
  const endpoint = `http://127.0.0.1:${port}`;
  const context = await browser.newContext({ baseURL: 'http://127.0.0.1:3010' });
  await context.route(/^https:\/\/fonts\.(googleapis|gstatic)\.com\//, route => route.abort());
  try {
    await expect.poll(async () => {
      try { return (await request.get(endpoint+'/api/health', { timeout: 1000 })).status(); } catch { return 0; }
    }, { timeout: 30000, message: errors }).toBe(200);
    const login = async (account: string) => {
      const response = await context.request.post(endpoint+'/api/auth/login', { data: { account, password: 'password123' } });
      expect(response.ok()).toBe(true);
    };
    await login('reader');
    await context.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      if (url.searchParams.get('stream')) return route.abort();
      const response = await route.fetch({ url: endpoint+url.pathname+url.search });
      await route.fulfill({ response });
    });
    const page = await context.newPage();
    await page.goto('/metrics');
    const snapshotReady = page.waitForResponse(response => new URL(response.url()).pathname === '/api/logistics/snapshots' && response.request().method() === 'POST');
    await page.getByRole('button', { name: '创建回放快照', exact: true }).click();
    await expect(page.getByText('当前结果：', { exact: false })).toBeVisible();
    const snapshot = await snapshotReady;
    expect(snapshot.ok()).toBe(true);
    const { snapshot_id } = await snapshot.json();
    const created = await context.request.post(endpoint+'/api/logistics/runs', { data: { snapshot_id, idempotency_key: 'collaboration-browser-api' } });
    expect(created.ok()).toBe(true);
    const { run_id } = await created.json();
    const path = `/runs/${run_id}`;
    await page.goto(path);
    const mutate = async (action: string, body: Record<string, unknown>) => {
      const response = await context.request.post(endpoint+'/api/logistics/runs/'+run_id+'/collaboration/'+action, { data: body });
      expect(response.ok()).toBe(true);
      return response.json();
    };
    // 旧调查仍可承接；既有反馈/建议接口保留，审批和处理使用新页面。
    const feedback = await mutate('feedback', { version: 0, kind: 'constraint', text: '演示约束：先核查两条任务', task_limit: 2, simulated: true });
    await mutate('propose', { version: feedback.version, strategy: 'verify_window', note: '先核查两条任务' });
    await page.goto(path);
    await page.getByRole('button', { name: '关联原调查并打开会话' }).click();
    await page.waitForURL(/\/assistant\/LCONV-/);
    const chatPath = new URL(page.url()).pathname + new URL(page.url()).search;
    const panel = page.getByRole('region', { name: '调查处理' });
    await expect(panel).toContainText('提交 v1 · 待审核');
    await expect(panel.getByRole('button', { name: '批准当前建议' })).toHaveCount(0);
    await login('reviewer');
    await page.goto('/approvals');
    await page.getByRole('button', { name: '核对建议与证据' }).first().click();
    const approval = page.getByRole('region', { name: '审批详情' });
    await approval.getByLabel('审核理由').fill('先核对窗口，再联系快递员');
    await approval.getByLabel('已核对提交版本 v1 及相关证据').check();
    await approval.getByRole('button', { name: '驳回当前建议' }).click();
    await expect(approval).toContainText('已驳回');
    await login('reader');
    const state = await context.request.get(endpoint+'/api/logistics/runs/'+run_id+'/collaboration');
    await mutate('propose', { version: (await state.json()).version, strategy: 'verify_window', note: '根据驳回原因，先核对窗口和两条任务' });
    await page.goto(chatPath);
    await expect(panel).toContainText('提交 v2 · 待审核');
    await login('reviewer'); await page.goto('/approvals');
    await page.getByRole('button', { name: '核对建议与证据' }).first().click();
    await approval.getByLabel('审核理由').fill('已核对当前版本、约束和证据，批准内部核查');
    await approval.getByLabel('已核对提交版本 v2 及相关证据').check();
    await approval.getByRole('button', { name: '批准当前建议' }).click();
    await expect(approval).toContainText('已批准，尚未创建跟进单');
    await page.getByRole('button', { name: '审批历史', exact: true }).click();
    await expect(page.locator('.lg-approval-list')).toContainText('v1');
    await expect(page.locator('.lg-approval-list')).toContainText('v2');
    await login('reader'); await page.goto(chatPath);
    await panel.getByRole('button', { name: '创建内部跟进单' }).click();
    await panel.getByLabel('回执说明').fill('演示模拟：已记录内部窗口核查结果');
    await panel.getByRole('button', { name: '保存跟进回执' }).click();
    await expect(panel.getByText('模拟回执', { exact: false })).toBeVisible();
    await panel.getByLabel('复核参考时点').fill('2000-07-02T10:30');
    await panel.getByRole('button', { name: '创建新快照并复核' }).click();
    await expect(panel.getByRole('region', { name: '复核结果' })).toContainText('旧风险队列');
    await expect(panel.getByRole('region', { name: '复核结果' })).toContainText('两时点之间新流入');
    await expect(panel.getByRole('region', { name: '复核结果' })).toContainText('不能归因于');
    await page.reload();
    await expect(panel.getByRole('region', { name: '复核结果' })).toBeVisible();
    await panel.scrollIntoViewIfNeeded();
    await page.screenshot({ path: info.outputPath('demo-loop-desktop.png'), fullPage: true, animations: 'disabled' });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole('button', { name: '当前调查与待办', exact: true }).click();
    await expect(panel).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath('demo-loop-mobile.png'), fullPage: true, animations: 'disabled' });
  } finally {
    await context.close(); child.stdin.end('\n');
    await new Promise<void>(resolve => {
      const timer = setTimeout(() => { child.kill(); resolve(); }, 10000);
      child.once('exit', () => { clearTimeout(timer); resolve(); });
    });
  }
});
// 浏览器回归只验证业务流程，外部字体不可用时使用页面原有本地回退。
test.beforeEach(async ({ context }) => {
  await context.route(/^https:\/\/fonts\.(googleapis|gstatic)\.com\//, route => route.abort());
});
