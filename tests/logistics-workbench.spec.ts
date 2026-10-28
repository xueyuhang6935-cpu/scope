import { expect, test } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.route('**/api/logistics/approvals/todos?**', route => route.fulfill({ json: { categories: [], rows: [], total_rows: 0, kind: '', scope_label: '当前账号全部授权记录，不按当前快照筛选' } }));
});

test('回放输入、四指标、任务展开、跨页搜索与撤权清除', async ({ page }, info) => {
  let version = 1; let regions = ['5', '6']; const bodies: unknown[] = []; const sourceRequests: string[] = [];
  await page.route('**/api/auth/session', route => route.fulfill({ json: { user: { name: '回放测试' } } }));
  await page.route('**/api/logistics/access/me', route => route.fulfill({ json: { account: 'reader', version, enabled: 1,
    roles: [{ role: 'investigator', city: 'Shanghai', regions }], roleCatalog: { investigator: '调查员' }, regionCatalog: ['5','6','8'] } }));
  await page.route('**/api/logistics/snapshots/config', route => route.fulfill({ json: { configured: true, batch_id: 'batch', city: 'Shanghai', region_ids: regions } }));
  await page.route('**/api/logistics/snapshots', route => {
    bodies.push(route.request().postDataJSON());
    return route.fulfill({ json: { snapshot_id: 'SNP-test', business_as_of: '2000-07-02 10:00:00', region_ids: ['5'], city: 'Shanghai' } });
  });
  const metric = (name: string, value: number | null) => ({ name, value, status: value == null ? 'unavailable' : 'ok', known_lower_bound: 1 });
  await page.route('**/api/logistics/snapshots/SNP-test/summary', route => route.fulfill({ json: { status: 'unavailable', missing_window_count: 1,
    metrics: { backlog: metric('积压', 21), overdue: metric('逾期', null), due_soon: metric('临期', 0), recent_accepted: metric('接单', 3), recent_completed: metric('完成', 2) },
    limitations: ['历史已完成任务样本，积压不代表生产全量未完成任务。'] } }));
  await page.route('**/api/logistics/snapshots/SNP-test/tasks?**', route => {
    const params = new URL(route.request().url()).searchParams;
    const second = params.get('page') === '2';
    const rows = params.get('search') === '不存在' ? [] : [{ row_index: second ? 1 : 0, order_id: second ? 'TASK-SECOND' : 'TASK-FIRST', region_id: '5', courier_id: '001', aoi_id: 'X',
      accept_time_reference: '2000-07-01 23:00:00', time_window_end_reference: '2000-07-01 23:30:00', current_wait_minutes: 660, current_overdue_minutes: 630, risk_state: 'overdue' }];
    return route.fulfill({ json: { total_rows: rows.length ? 21 : 0, page_size: 20, status: 'ok', rows } });
  });
  page.on('request', request => { if (request.url().includes('/data/preview')) sourceRequests.push(request.url()); });
  await page.goto('/metrics');
  await expect(page.getByRole('navigation', { name: '面包屑' })).toHaveText('上海运营空间风险中心');
  await expect(page.getByRole('combobox', { name: '回放区域' }).locator('option')).toHaveCount(3);
  const regionSelector = page.getByRole('combobox', { name: '运营区域', exact: true });
  expect(await regionSelector.locator('option').evaluateAll(options => options.map(option => (option as HTMLOptionElement).value))).toEqual(['all', '5', '6']);
  await regionSelector.selectOption('all');
  await expect(page.getByLabel('回放区域')).toHaveValue('all');
  await regionSelector.selectOption('5');
  await page.getByRole('button', { name: '创建回放快照', exact: true }).click();
  await expect(page.locator('.lg-metric-card').filter({ hasText: '样本内积压' })).toContainText('21');
  await expect(page.locator('.lg-metric-card').filter({ hasText: '逾期未完成' })).toContainText('不可计算');
  await expect(page.locator('.lg-metric-grid .lg-metric-card')).toHaveCount(4);
  expect(bodies[0]).toEqual({ batch_id: 'batch', city: 'Shanghai', region_ids: ['5'], business_as_of: '2000-07-02 10:00:00' });
  await regionSelector.selectOption('6');
  await expect(page.getByLabel('回放区域')).toHaveValue('6');
  await expect(page.locator('.lg-metric-card').filter({ hasText: '样本内积压' })).toBeDisabled();
  await expect(page.getByText('当前结果：', { exact: false })).toHaveCount(0);
  await page.getByLabel('回放区域').selectOption('5');
  await expect(regionSelector).toHaveValue('5');
  await page.getByRole('button', { name: '创建回放快照', exact: true }).click();
  await expect(page.locator('.lg-metric-card').filter({ hasText: '样本内积压' })).toBeEnabled();
  await page.locator('.lg-metric-card').filter({ hasText: '样本内积压' }).click();
  await expect(page.getByRole('table')).toContainText('TASK-FIRST');
  await page.getByRole('button', { name: '下一页', exact: true }).click();
  await expect(page.getByRole('table')).toContainText('TASK-SECOND');
  await page.getByRole('textbox', { name: '搜索记录' }).fill('不存在');
  await expect(page.getByRole('table')).toContainText('没有匹配任务');
  await page.getByRole('textbox', { name: '搜索记录' }).fill('');
  await page.evaluate(() => { (document.activeElement as HTMLElement)?.blur(); window.scrollTo(0, 0); });
  await page.screenshot({ path: info.outputPath('workbench-desktop.png'), fullPage: true, animations: 'disabled' });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => window.scrollTo(0, 0));
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath('workbench-mobile.png'), fullPage: true, animations: 'disabled' });
  version++; regions = [];
  await expect(page.getByText('TASK-SECOND')).toHaveCount(0, { timeout: 10000 });
  await expect(regionSelector).toBeDisabled({ timeout: 10000 });
  await expect(regionSelector.locator('option')).toHaveCount(1);
  expect(sourceRequests).toEqual([]);
});

test('变更输入清空旧结果，非法年份不发起快照', async ({ page }) => {
  await page.route('**/api/auth/session', route => route.fulfill({ json: { user: { name: '测试' } } }));
  await page.route('**/api/logistics/access/me', route => route.fulfill({ json: { account: 'reader', version: 1, roles: [{ role: 'investigator', city: 'Shanghai', regions: ['5'] }], roleCatalog: {}, regionCatalog: ['5'] } }));
  await page.route('**/api/logistics/snapshots/config', route => route.fulfill({ json: { configured: true, batch_id: 'batch', city: 'Shanghai', region_ids: ['5'] } }));
  let requests = 0;
  await page.route('**/api/logistics/snapshots', route => { requests++; return route.fulfill({ status: 409, json: { message: '批次校验失败' } }); });
  await page.goto('/metrics');
  await expect(page.getByRole('button', { name: '创建回放快照', exact: true })).toBeEnabled();
  await page.getByLabel('回放参考时点').fill('2026-07-02T10:00');
  await page.getByRole('button', { name: '创建回放快照', exact: true }).click();
  expect(requests).toBe(0);
  await page.getByLabel('回放参考时点').fill('2000-07-02T10:00');
  await page.getByRole('button', { name: '创建回放快照', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('批次校验失败');
  await expect(page.locator('.lg-metric-card').filter({ hasText: '样本内积压' })).toBeDisabled();
});

test('工作台移除调查与历史入口，浏览和刷新不查询或创建调查', async ({ page }, info) => {
  await page.route('**/api/auth/session', route => route.fulfill({ json: { user: { name: '测试' } } }));
  await page.route('**/api/logistics/access/me', route => route.fulfill({ json: { account: 'reader', version: 1,
    roles: [{ role: 'investigator', city: 'Shanghai', regions: ['5'] }], roleCatalog: {}, regionCatalog: ['5'] } }));
  await page.route('**/api/logistics/snapshots/config', route => route.fulfill({ json: { configured: true, batch_id: 'batch', city: 'Shanghai', region_ids: ['5'] } }));
  await page.route('**/api/logistics/snapshots', route => route.fulfill({ json: { snapshot_id: 'SNP-demo', business_as_of: '2000-07-02 10:00:00', city: 'Shanghai', region_ids: ['5'] } }));
  const metric = { value: 0, status: 'ok' };
  await page.route('**/api/logistics/snapshots/SNP-demo/summary', route => route.fulfill({ json: { status: 'ok', limitations: [], missing_window_count: 0, horizon_minutes: 30,
    metrics: { backlog: metric, overdue: metric, due_soon: metric, recent_accepted: metric, recent_completed: metric } } }));
  await page.route('**/api/logistics/snapshots/SNP-demo/tasks?**', route => route.fulfill({ json: { rows: [], total_rows: 0, page_size: 20, status: 'empty' } }));
  const runRequests: string[] = [];
  page.on('request', request => { if (new URL(request.url()).pathname === '/api/logistics/runs') runRequests.push(request.method()); });
  await page.clock.install();
  await page.goto('/metrics');
  async function checkEntries() {
    await expect(page.getByRole('button', { name: '配置展示指标', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: /发起调查|启动.*调查/ })).toHaveCount(0);
    await expect(page.getByLabel('调查方式')).toHaveCount(0);
    await expect(page.getByLabel('调查问题')).toHaveCount(0);
    await expect(page.getByRole('heading', { name: '最近调查', exact: true })).toHaveCount(0);
    await expect(page.getByRole('link', { name: /查看运行记录|查看调查记录|查看全部$/ })).toHaveCount(0);
    await expect(page.getByRole('button', { name: '数据中心', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: '需要处理', exact: true })).toHaveCount(0);
    await expect(page.getByRole('link', { name: '运行记录', exact: true })).toHaveCount(0);
  }
  await checkEntries();
  await page.getByRole('button', { name: '创建回放快照', exact: true }).click();
  await expect(page.getByRole('region', { name: '风险提醒', exact: true })).toContainText('区域 5');
  await page.getByRole('button', { name: '逾期', exact: true }).click();
  await expect(page.getByRole('button', { name: '刷新账号工作流', exact: true })).toHaveCount(0);
  await page.clock.fastForward(11000);
  await checkEntries();
  expect(runRequests).toEqual([]);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => { (document.activeElement as HTMLElement)?.blur(); window.scrollTo(0, 0); });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath('workbench-entries-mobile.png'), fullPage: true, animations: 'disabled' });
  await page.reload();
  await checkEntries();
  expect(runRequests).toEqual([]);
});
// 浏览器回归只验证业务流程，外部字体不可用时使用页面原有本地回退。
test.beforeEach(async ({ context }) => {
  await context.route(/^https:\/\/fonts\.(googleapis|gstatic)\.com\//, route => route.abort());
});
