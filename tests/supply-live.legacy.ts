import { test, expect } from '@playwright/test';
import { execFileSync, spawn, type ChildProcess } from 'node:child_process';
import { mkdirSync, mkdtempSync } from 'node:fs';
import path from 'node:path';

let backend: ChildProcess;
let root: string;
const api = 'http://127.0.0.1:3012';
test.beforeAll(async ({ request }) => {
  mkdirSync('test-results', { recursive: true });
  root = mkdtempSync(path.resolve('test-results/.supply-e2e-'));
  const script = `import os,uvicorn
from backend.main import create_app
from backend.auth import now,password_hash
app=create_app({'AUTH_DB_PATH':os.path.join(os.environ['SUPPLY_TEST_DIR'],'auth.sqlite'),'DATA_DB_PATH':os.path.join(os.environ['SUPPLY_TEST_DIR'],'supply.sqlite'),'SUPPLY_PLANNER':'rules','SUPPLY_INVESTIGATOR':'rules','NODE_ENV':'test'})
for account in ('supply@test.invalid','database@test.invalid','risk@test.invalid','workflow@test.invalid','approver@test.invalid','roles-owner@test.invalid','roles-member@test.invalid'):
    app.state.auth.query("INSERT INTO users(account,type,name,password_hash,created_at) VALUES(?,'email',?,?,?)",(account,'Supply test',password_hash('password123'),now()))
uvicorn.run(app,host='127.0.0.1',port=3012)
`;
  backend = spawn(path.resolve('.venv/Scripts/python.exe'), ['-c', script], { env: { ...process.env, SUPPLY_TEST_DIR: root }, windowsHide: true, stdio: 'ignore' });
  await expect.poll(async () => { try { return (await request.get(`${api}/api/health`)).status(); } catch { return 0; } }, { timeout: 20000 }).toBe(200);
});
test.afterAll(async () => {
  if (backend && backend.exitCode === null) { const stopped = new Promise(resolve => backend.once('exit', resolve)); backend.kill(); await stopped; }
  if (root) execFileSync(path.resolve('.venv/Scripts/python.exe'), ['-c', `
import os,sqlite3
from dotenv import dotenv_values
from backend.database_connection import connection
local=sqlite3.connect(os.path.join(os.environ['SUPPLY_TEST_DIR'],'supply.sqlite'))
keys=[r[0] for r in local.execute('SELECT workspace_id FROM risk_monitor_configs')]
local.close()
with connection(dotenv_values('.env'),readonly=False) as db:
    if db.execute("SELECT to_regclass('sc.risk_events') AS t").fetchone()['t']:
        for key in keys:
            if db.execute("SELECT to_regclass('sc.investigations') AS t").fetchone()['t']:
                db.execute('DELETE FROM sc.plan_approvals WHERE plan_id IN (SELECT p.id FROM sc.candidate_plans p JOIN sc.investigations r ON r.id=p.run_id WHERE r.workspace_id=%s)',(key,))
                db.execute('DELETE FROM sc.candidate_plans WHERE run_id IN (SELECT id FROM sc.investigations WHERE workspace_id=%s)',(key,))
                db.execute('DELETE FROM sc.investigations WHERE workspace_id=%s',(key,))
            db.execute('DELETE FROM sc.risk_history WHERE event_id IN (SELECT id FROM sc.risk_events WHERE workspace_id=%s)',(key,))
            db.execute('DELETE FROM sc.risk_events WHERE workspace_id=%s',(key,))
            db.execute('DELETE FROM sc.risk_scans WHERE workspace_id=%s',(key,))
            db.execute('DELETE FROM sc.commitment_changes WHERE workspace_id=%s',(key,))
`], { env: { ...process.env, SUPPLY_TEST_DIR: root }, windowsHide: true });
  if (root && path.resolve(root).startsWith(path.resolve('test-results') + path.sep)) execFileSync(path.resolve('.venv/Scripts/python.exe'), ['-c', "import os,shutil; from pathlib import Path; p=Path(os.environ['SUPPLY_TEST_DIR']).resolve(); assert p.is_relative_to(Path('test-results').resolve()) and p.name.startswith('.supply-e2e-'); shutil.rmtree(p)"], { env: { ...process.env, SUPPLY_TEST_DIR: root }, windowsHide: true });
});

test('调查试算、跨账号权限与持久化审批', async ({ page }, testInfo) => {
  await page.route('https://fonts.googleapis.com/**', route => route.abort());
  await page.route('https://fonts.gstatic.com/**', route => route.abort());
  await page.route('**/api/**', async route => { const url = new URL(route.request().url()); const response = await route.fetch({ url: api + url.pathname + url.search }); await route.fulfill({ response }); });
  expect((await page.request.post(`${api}/api/auth/login`, { data: { account: 'workflow@test.invalid', password: 'password123' } })).ok()).toBe(true);
  expect((await page.request.post(`${api}/api/data/database`, { data: { scenarioId: 'partial_transfer' } })).ok()).toBe(true);
  expect((await page.request.post(`${api}/api/supply/monitor`, { data: { days: 3, enabled: false } })).ok()).toBe(true);
  await page.goto('/risks?factory=A');
  await page.getByRole('button', { name: '立即扫描', exact: true }).click();
  await page.getByRole('link', { name: '查看事件', exact: true }).click();
  const eventUrl = page.url();
  await page.getByRole('button', { name: '调查并试算', exact: true }).click();
  const plans = page.getByRole('table', { name: '处置方案比较', exact: true });
  await expect(plans).toContainText('调拨＋加急');
  await expect(plans).toContainText('3500');
  await page.getByText('业务成员与审批权限', { exact: true }).click();
  await page.getByLabel('业务账号', { exact: true }).fill('approver@test.invalid');
  await page.getByRole('button', { name: '保存业务权限', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('权限已保存');
  await page.screenshot({ path: testInfo.outputPath('investigation-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => page.locator('.sp-sidebar').evaluate(el => el.getBoundingClientRect().right)).toBeLessThanOrEqual(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('investigation-mobile.png'), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  expect((await page.request.post(`${api}/api/auth/login`, { data: { account: 'approver@test.invalid', password: 'password123' } })).ok()).toBe(true);
  await page.goto('/approvals');
  await page.getByRole('row').filter({ hasText: '跨厂调拨' }).getByRole('link', { name: '核对方案' }).click();
  await page.getByLabel('审批意见').fill('单独调拨覆盖不足，请重新比较其他方案。');
  await page.getByRole('button', { name: '驳回方案', exact: true }).click();
  await expect(page.getByText('审批记录：', { exact: false })).toContainText('已驳回');
  await page.goto('/approvals');
  await page.getByLabel('协作风险', { exact: true }).selectOption(new URL(eventUrl).pathname.split('/').pop()!);
  const investigation = page.getByRole('region', { name: '调查与方案试算' });
  await expect(investigation).toContainText('自动重规划第 1 轮');
  await expect(investigation).toContainText('单独调拨覆盖不足');
  const alternatives = investigation.getByRole('table', { name: '处置方案比较', exact: true }).first();
  await expect(alternatives.getByRole('row').filter({ hasText: '跨厂调拨' })).toContainText('不可行');
  await page.screenshot({ path: testInfo.outputPath('automatic-replanning.png'), fullPage: true });
  await alternatives.getByRole('row').filter({ hasText: '调拨＋加急' }).getByRole('link', { name: '核对方案' }).click();
  await expect(page.getByRole('heading', { name: '调拨＋加急 · v6' })).toBeVisible();
  await page.getByLabel('审批意见').fill('已核对两周来源余量、组合覆盖量及费用。');
  await page.getByRole('button', { name: '批准方案', exact: true }).click();
  await expect(page.getByText('审批记录：', { exact: false })).toContainText('已批准');
  await page.reload();
  await expect(page.getByText('审批记录：', { exact: false })).toContainText('approver@test.invalid');
  await page.screenshot({ path: testInfo.outputPath('approval-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => page.locator('.sp-sidebar').evaluate(el => el.getBoundingClientRect().right)).toBeLessThanOrEqual(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('approval-mobile.png'), fullPage: true });
  expect((await page.request.post(`${api}/api/auth/login`, { data: { account: 'workflow@test.invalid', password: 'password123' } })).ok()).toBe(true);
  await page.goto(eventUrl);
  await expect(page.getByRole('region', { name: '调查与方案试算' })).toContainText('已批准');
});

test('多角色管理、工厂范围、停用与审计', async ({ page }, testInfo) => {
  await page.route('https://fonts.googleapis.com/**', route => route.abort());
  await page.route('https://fonts.gstatic.com/**', route => route.abort());
  await page.route('**/api/**', async route => { const url = new URL(route.request().url()); const response = await route.fetch({ url: api + url.pathname + url.search }); await route.fulfill({ response }); });
  expect((await page.request.post(`${api}/api/auth/login`, { data: { account: 'roles-owner@test.invalid', password: 'password123' } })).ok()).toBe(true);
  const own = await (await page.request.get(`${api}/api/supply/access/me`)).json();
  await page.goto('/settings?tab=permissions');
  await expect(page.getByRole('table', { name: '工作空间成员' })).toContainText('roles-owner@test.invalid');
  await page.getByLabel('成员账号', { exact: true }).fill('roles-member@test.invalid');
  await page.getByLabel('授予生产计划员', { exact: true }).check();
  await page.getByLabel('生产计划员-B厂', { exact: true }).uncheck();
  await page.getByLabel('授予审批人', { exact: true }).check();
  await page.getByLabel('审批人-B厂', { exact: true }).uncheck();
  await page.getByLabel('成员审批额度', { exact: true }).fill('500');
  await page.getByRole('button', { name: '保存成员权限', exact: true }).click();
  await expect(page.getByRole('table', { name: '工作空间成员' })).toContainText('额度 500 元');
  await page.getByRole('button', { name: '编辑 roles-member@test.invalid', exact: true }).click();
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: testInfo.outputPath('roles-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => page.locator('.sp-sidebar').evaluate(el => el.getBoundingClientRect().right)).toBeLessThanOrEqual(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: testInfo.outputPath('roles-mobile.png'), fullPage: true });
  expect((await page.request.post(`${api}/api/auth/login`, { data: { account: 'roles-member@test.invalid', password: 'password123' } })).ok()).toBe(true);
  await page.evaluate(workspace => sessionStorage.setItem('business-workspace', workspace), own.workspaceId);
  await page.goto('/settings?tab=permissions');
  await expect(page.getByText('当前账号无成员管理权限。请联系该工作空间的系统管理员。')).toBeVisible();
  await expect(page.getByRole('button', { name: '保存成员权限', exact: true })).toHaveCount(0);
  expect((await page.request.post(`${api}/api/supply/access/members`, { headers: { 'X-Workspace-Id': own.workspaceId }, data: { account: 'roles-member@test.invalid', roles: [{ role: 'system_admin', factories: ['A', 'B'], domains: [] }], version: 1 } })).status()).toBe(403);
  await page.goto('/data/connections?factory=A');
  await page.getByRole('button', { name: '配置数据库', exact: true }).click();
  await expect(page.getByRole('button', { name: '保存并接入', exact: true })).toBeDisabled();
  expect((await page.request.post(`${api}/api/auth/login`, { data: { account: 'roles-owner@test.invalid', password: 'password123' } })).ok()).toBe(true);
  await page.goto('/settings?tab=permissions');
  await page.getByRole('button', { name: '编辑 roles-member@test.invalid', exact: true }).click();
  await page.getByLabel('启用成员', { exact: true }).uncheck();
  await page.getByRole('button', { name: '保存成员权限', exact: true }).click();
  await expect(page.getByRole('table', { name: '工作空间成员' })).toContainText('停用');
  await expect(page.getByRole('heading', { name: '权限与规则变更记录' })).toBeVisible();
  expect((await page.request.post(`${api}/api/auth/login`, { data: { account: 'roles-member@test.invalid', password: 'password123' } })).ok()).toBe(true);
  expect((await page.request.get(`${api}/api/supply/access/me`, { headers: { 'X-Workspace-Id': own.workspaceId } })).status()).toBe(403);
});

test('真实缺料扫描、交期变更、事件合并、助手计算与刷新', async ({ page }, testInfo) => {
  await page.route('https://fonts.googleapis.com/**', route => route.abort());
  await page.route('https://fonts.gstatic.com/**', route => route.abort());
  await page.route('**/api/**', async route => { const url = new URL(route.request().url()); const response = await route.fetch({ url: api + url.pathname + url.search }); await route.fulfill({ response }); });
  expect((await page.request.post(`${api}/api/auth/login`, { data: { account: 'risk@test.invalid', password: 'password123' } })).ok()).toBe(true);
  expect((await page.request.post(`${api}/api/data/database`, { data: { scenarioId: 'supplier_delay' } })).ok()).toBe(true);
  expect((await page.request.post(`${api}/api/supply/monitor`, { data: { days: 3, enabled: false } })).ok()).toBe(true);
  await page.goto('/workspace?factory=A');
  await page.getByRole('button', { name: '立即扫描', exact: true }).click();
  const table = page.getByRole('table', { name: '风险事件列表' });
  await expect(table).toContainText('400 件');
  await expect(table.locator('tbody tr')).toHaveCount(1);
  await page.getByRole('button', { name: '立即扫描', exact: true }).click();
  await expect(table.locator('tbody tr')).toHaveCount(1);
  await page.getByRole('link', { name: '查看事件', exact: true }).click();
  const eventUrl = page.url();
  await expect(page.getByRole('img', { name: '预计库存结余曲线' })).toBeVisible();
  await expect(page.getByRole('table', { name: '受影响生产计划', exact: true })).toContainText('PLAN-A-CAR-1-2');
  await page.getByLabel('新负责人').fill('联调计划员');
  await page.getByRole('button', { name: '分配负责人' }).click();
  await expect(page.getByText('负责人：联调计划员', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '确认风险', exact: true }).click();
  await expect(page.getByRole('button', { name: '恢复待处理' })).toBeVisible();
  await page.reload();
  await expect(page.getByText('负责人：联调计划员', { exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('risk-detail-desktop.png'), fullPage: true });
  await page.getByRole('link', { name: '返回列表', exact: true }).click();
  await page.getByText('监测设置与交期变更', { exact: true }).click();
  await page.getByLabel('新承诺到线时间', { exact: true }).fill('2026-10-06T08:00');
  await page.getByRole('button', { name: '提交交期变更并扫描' }).click();
  await expect(table).toContainText('当前窗口已覆盖');
  await page.getByLabel('新承诺到线时间', { exact: true }).fill('2026-10-08T08:00');
  await page.getByRole('button', { name: '提交交期变更并扫描' }).click();
  await expect(table.locator('tbody tr')).toHaveCount(2);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => page.locator('.sp-sidebar').evaluate(el => el.getBoundingClientRect().right)).toBeLessThanOrEqual(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('risk-workspace-mobile.png'), fullPage: true });
  await page.goto(eventUrl);
  await expect(page.getByRole('heading', { name: '缺料影响', exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('risk-detail-mobile.png'), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/assistant?factory=A');
  await page.getByLabel('供应链消息草稿').fill('检查 P001 未来三天缺料风险');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await expect(page.getByLabel('真实分析回答')).toContainText('supplier_delay');
  await expect(page.getByRole('button', { name: '按此范围查询' })).toHaveCount(0);
  await expect(page.getByRole('table', { name: '缺料计算结果' })).toContainText('400 件');
});

test('PostgreSQL 真实连接、场景保存、数据预览及刷新', async ({ page }, testInfo) => {
  await page.route('https://fonts.googleapis.com/**', route => route.abort());
  await page.route('https://fonts.gstatic.com/**', route => route.abort());
  await page.route('**/api/**', async route => { const url = new URL(route.request().url()); const response = await route.fetch({ url: api + url.pathname + url.search }); await route.fulfill({ response }); });
  expect((await page.request.post(`${api}/api/auth/login`, { data: { account: 'database@test.invalid', password: 'password123' } })).ok()).toBe(true);
  await page.goto('/data/connections?factory=A');
  await page.getByRole('button', { name: '配置数据库', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '配置数据库' });
  await expect(dialog).toContainText('scope_analyst / supply_demo');
  await page.getByLabel('数据场景').selectOption('supplier_delay');
  await page.getByRole('button', { name: '测试连接', exact: true }).click();
  await expect(dialog).toContainText('连接测试成功');
  await page.getByRole('button', { name: '保存并接入', exact: true }).click();
  await expect(dialog).toContainText('共 12 条记录');
  await expect(page.getByRole('table', { name: 'PostgreSQL 数据记录' })).toContainText('supplier_delay');
  const bounds = await dialog.boundingBox();
  expect(Math.abs(bounds!.x + bounds!.width / 2 - (202 + (1440 - 202) / 2))).toBeLessThan(3);
  await page.screenshot({ path: testInfo.outputPath('database-desktop.png'), fullPage: true });
  await dialog.getByRole('button', { name: '关闭', exact: true }).click();
  await expect(page.locator('.sp-connection-card').filter({ hasText: '数据库连接' })).toContainText('已连接');
  await page.reload();
  await page.getByRole('button', { name: '管理数据库', exact: true }).click();
  await expect(page.getByLabel('数据场景')).toHaveValue('supplier_delay');
  await page.getByLabel('数据表').selectOption('production_plan_lines');
  await expect(dialog).toContainText('第 1 / 2 页');
  await page.getByRole('button', { name: '下一页', exact: true }).click();
  await expect(dialog).toContainText('第 2 / 2 页');
  await page.getByLabel('数据场景').selectOption('quality_hold');
  await expect(page.getByRole('region', { name: '数据库数据预览' })).toHaveCount(0);
  await page.getByRole('button', { name: '保存并接入', exact: true }).click();
  await page.getByLabel('数据表').selectOption('quality_holds');
  await expect(dialog).toContainText('共 1 条记录');
  await expect(page.getByRole('table', { name: 'PostgreSQL 数据记录' })).toContainText('quality_hold');
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('database-mobile.png'), fullPage: true });
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
});

test('真实导入到直接查询、澄清、证据、刷新及取消闭环', async ({ page }, testInfo) => {
  await page.route('https://fonts.googleapis.com/**', route => route.abort());
  await page.route('https://fonts.gstatic.com/**', route => route.abort());
  await page.route('**/api/**', async route => { const url = new URL(route.request().url()); const response = await route.fetch({ url: api + url.pathname + url.search }); await route.fulfill({ response }); });
  const login = await page.request.post(`${api}/api/auth/login`, { data: { account: 'supply@test.invalid', password: 'password123' } });
  expect(login.ok()).toBe(true);
  const upload = await page.request.post(`${api}/api/data/imports/preview`, { multipart: { domain: 'inventory', factory: 'A', owner: 'Test', file: { name: 'stock.csv', mimeType: 'text/csv', buffer: Buffer.from('material,quantity,unit\nP001,125,件') } } });
  const { draftId } = await upload.json();
  expect((await page.request.post(`${api}/api/data/imports/${draftId}/check`, { data: { mapping: { material: 'material', quantity: 'quantity', unit: 'unit' } } })).ok()).toBe(true);
  expect((await page.request.post(`${api}/api/data/imports/${draftId}/commit`, { data: { strategy: 'insert', businessKey: 'material', idempotencyKey: crypto.randomUUID() } })).ok()).toBe(true);
  await page.goto('/assistant?factory=A');
  await page.getByRole('button', { name: '查询 P001 库存' }).click();
  await page.route('**/api/supply/threads/*/messages', async route => { const url = new URL(route.request().url()); await route.fetch({ url: api + url.pathname }); await route.abort('failed'); }, { times: 1 });
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await expect(page.locator('.sp-composer [role=alert]')).toBeVisible();
  await expect(page.getByLabel('供应链消息草稿')).toHaveValue('查询 P001 库存');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await expect(page).toHaveURL(/assistant\/CHAT-/);
  await expect(page.getByLabel('真实分析回答')).toContainText('125 件');
  await expect(page.getByRole('button', { name: '按此范围查询' })).toHaveCount(0);
  await expect(page.getByLabel('真实分析回答').getByText('当次执行范围')).toBeVisible();
  const url = page.url();
  await page.reload();
  await expect(page.getByLabel('真实分析回答')).toContainText('125 件');
  await expect(page.getByLabel('真实分析回答')).toHaveCount(1);
  await expect(page.getByLabel('供应链消息草稿')).toHaveValue('');
  await page.screenshot({ path: testInfo.outputPath('live-analysis-desktop.png'), fullPage: true });
  await page.getByLabel('供应链消息草稿').fill('检查未来三天缺料风险');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await expect(page.getByLabel('真实分析回答').last()).toContainText('尚未接入缺料计算场景');
  await page.getByLabel('确认分析类型').selectOption('inventory');
  await page.getByLabel('确认时间范围').selectOption('');
  await page.getByRole('button', { name: '按此范围查询' }).click();
  await expect(page.getByLabel('真实分析回答').last()).toContainText('125 件');
  await page.getByLabel('供应链消息草稿').fill('P001');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await expect(page.getByLabel('真实分析回答').last()).toContainText('请选择分析类型');
  await page.getByRole('button', { name: '停止任务' }).click();
  await expect(page.getByLabel('真实分析回答').last()).toContainText('已取消');
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => page.locator('.sp-sidebar').evaluate(el => el.getBoundingClientRect().right)).toBeLessThanOrEqual(0);
  await expect(page.getByLabel('供应链消息草稿')).toBeInViewport();
  await page.getByLabel('真实分析回答').last().scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('live-analysis-mobile.png'), fullPage: true });
  expect(page.url()).toBe(url);
});
