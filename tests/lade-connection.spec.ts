import { expect, test } from '@playwright/test';

test('揽收数据库连接、批次保存及任务溯源预览', async ({ page }, testInfo) => {
  const batch = 'pickup_sh_ebc564f1b6235e09ccca7cf4ccfe8a2014c91e99d973516e69350da9f7424db9';
  const connection = { configured: false, connected: true, sourceType: 'lade', schema: 'lade',
    host: '127.0.0.1', port: '55432', database: 'scope_analyst', scenarioId: batch, defaultTable: 'pickup_tasks',
    tables: ['import_batches', 'pickup_tasks', 'quality_rules'], scenarios: [{ id: batch, name: '上海揽收 · 1,424,406 条 · 7424db9' }] };
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/auth/session') return route.fulfill({ json: { user: { name: '测试数据管理员', account: 'lade@test.invalid' } } });
    if (url.pathname === '/api/supply/access/me') return route.fulfill({ json: {
      account: 'lade@test.invalid', workspaceId: 'test-lade', workspaces: [{ id: 'test-lade', name: '测试空间' }],
      roles: [{ role: 'data_admin', factories: ['A', 'B'], domains: ['inventory', 'rules'], approval_limit: '0' }],
      capabilities: {}, roleCatalog: {}, domains: [],
    } });
    if (url.pathname === '/api/data/database') {
      if (route.request().method() === 'POST') {
        expect(route.request().postDataJSON()).toEqual({ scenarioId: batch });
        connection.configured = true;
      }
      return route.fulfill({ json: connection });
    }
    if (url.pathname === '/api/data/database/test') return route.fulfill({ json: { message: '连接测试成功，揽收任务和质量标记可读取。' } });
    if (url.pathname === '/api/data/database/preview') {
      expect(url.searchParams.get('table')).toBe('pickup_tasks');
      expect(url.searchParams.get('factory')).toBe('all');
      const filtered = url.searchParams.get('order_id') === '2590170';
      if (filtered) expect(url.searchParams.get('region')).toBe('43');
      return route.fulfill({ json: { columns: ['order_id', 'source_row', 'accept_time', 'pickup_time', 'quality_flags'],
        rows: [{ order_id: '2590170', source_row: 968475, accept_time: '05-26 13:03:00', pickup_time: '07-03 15:22:00', quality_flags: ['cross_day_task', 'pickup_after_window'] }],
        total: filtered ? 1 : 1424406, pageSize: 20, factoryScoped: false } });
    }
    return route.fulfill({ json: [] });
  });
  await page.goto('/data/connections?factory=B');
  const card = page.locator('.sp-connection-card').filter({ hasText: '数据库连接' });
  await expect(card).toContainText('已连接');
  await card.getByRole('button', { name: '配置数据库' }).click();
  const modal = page.getByRole('dialog', { name: '配置数据库' });
  await expect(modal.getByLabel('导入批次')).toHaveValue(batch);
  await expect(modal.getByRole('table')).toContainText('968475');
  await expect(modal).not.toContainText('当前数据为模拟数据');
  await modal.getByRole('button', { name: '测试连接' }).click();
  await expect(modal).toContainText('连接测试成功');
  await modal.getByRole('button', { name: '保存并接入' }).click();
  await expect(modal).toContainText('配置已保存');
  await modal.getByLabel('区域编号').fill('43');
  await modal.getByLabel('任务编号').fill('2590170');
  await modal.getByRole('button', { name: '查询任务' }).click();
  await expect(modal).toContainText('共 1 条记录');
  await expect(modal.getByRole('table')).toContainText('pickup_after_window');
  await page.screenshot({ path: testInfo.outputPath('lade-connection.png'), fullPage: true });
});
