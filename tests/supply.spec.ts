import { expect, test } from '@playwright/test';

const mockRisks = Array.from({ length: 11 }, (_, i) => ({
  id: `RISK-UI-${i}`, factory: i === 10 ? 'B' : 'A', material: i === 10 ? 'P012' : `P${String(i + 1).padStart(3, '0')}`,
  revision: 'R1', owner: '测试计划员', status: i === 10 ? 'data_required' : 'open', version: 1, recurrence_of: null,
  scenario_id: 'supplier_delay', updated_at: '2026-10-05T07:00:00+08:00',
  result: { name: i === 10 ? '单位换算待补充' : '模拟物料', factory: i === 10 ? 'B' : 'A', computable: i !== 10, shortage: i === 10 ? null : '400', opening: '800', totalDemand: '1200', unit: '件', revision: 'R1', firstShortageAt: '2026-10-07T16:00:00+08:00', missing: [], timeline: [], affectedPlans: [], evidenceIds: [], safetyWarnings: [] },
}));

test.beforeEach(async ({ context }) => {
  await context.route('**/api/supply/**', route => route.fulfill({ status: 503, json: { message: '测试服务未启用' } }));
  await context.route('**/api/supply/access/me', route => route.fulfill({ json: { account: 'test', workspaceId: 'test', workspaces: [{ id: 'test', name: '测试空间' }], roles: ['planner', 'data_admin', 'system_admin'].map(role => ({ role, factories: ['A', 'B'], domains: ['inventory', 'purchase', 'rules'], approval_limit: '0' })), capabilities: { view: ['A', 'B'] }, roleCatalog: {}, domains: [] } }));
  await context.route('**/api/supply/monitor', route => route.fulfill({ json: { configured: true, scenario_id: 'supplier_delay', as_of: '2026-10-05T07:00:00+08:00', horizon_days: 7, enabled: 1, interval_seconds: 60, last_attempt: null } }));
  await context.route('**/api/supply/risks?*', route => {
    const factory = new URL(route.request().url()).searchParams.get('factory');
    return route.fulfill({ json: { events: mockRisks.filter(r => factory === 'all' || r.factory === factory), latestScan: null } });
  });
  await context.route('**/api/supply/risks/RISK-*', route => {
    const url = new URL(route.request().url());
    const item = mockRisks.find(r => r.id === url.pathname.split('/').at(-1));
    return route.fulfill({ json: { ...item, evidence: [], history: [], asOf: '2026-10-05T07:00:00+08:00', windowStart: '2026-10-05T00:00:00+08:00', windowEnd: '2026-10-08T00:00:00+08:00', snapshotHash: 'test-snapshot' } });
  });
  await context.route('https://fonts.googleapis.com/**', route => route.abort());
  await context.route('https://fonts.gstatic.com/**', route => route.abort());
  await context.route('**/api/auth/session', route => route.fulfill({ json: { user: { name: '测试计划员' } } }));
});

test('工作台、配色和工厂范围一致', async ({ page }, testInfo) => {
  await page.goto('/workspace');
  await expect(page.getByRole('heading', { name: '工作台', exact: true })).toBeVisible();
  await expect(page.locator('.sp-nav-item.is-active')).toHaveText('工作台');
  await expect(page.locator('.sp-primary').first()).toHaveCSS('background-color', 'rgb(102, 84, 197)');
  await expect(page.locator('.sp-primary').first()).toHaveCSS('color', 'rgb(255, 255, 255)');
  await expect(page.locator('tbody tr')).toHaveCount(10);
  await page.screenshot({ path: testInfo.outputPath('workspace-desktop.png'), fullPage: true });
  await page.getByLabel('工厂范围').selectOption('B');
  await expect(page.locator('tbody tr')).toHaveCount(1);
  await expect(page.locator('.sp-metric').first().locator('strong')).toHaveText('0');
  await expect(page.getByText('P012 · 单位换算待补充')).toBeVisible();
  await expect(page.getByText('P001 调拨方案 v1', { exact: true })).toHaveCount(0);
  await page.reload();
  await expect(page.getByLabel('工厂范围')).toHaveValue('B');
});

test('计算风险筛选、分页、详情刷新和返回列表', async ({ page, context }) => {
  await page.goto('/risks');
  await page.getByRole('button', { name: '下一页' }).click();
  await expect(page.locator('tbody tr')).toHaveCount(1);
  await page.getByRole('link', { name: '查看事件' }).click();
  await expect(page.locator('.sp-nav-item.is-active')).toHaveText('风险处置');
  await expect(page.getByRole('heading', { name: '来源证据', exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: '来源证据', exact: true })).toBeVisible();
  const newPage = await context.newPage();
  await newPage.goto(page.url());
  await expect(newPage.getByRole('heading', { name: /P012 ·/ })).toBeVisible();
  await newPage.close();
  await page.getByRole('link', { name: '返回列表', exact: true }).click();
  await expect(page).toHaveURL(/page=2/);
  await expect(page.getByText('共 11 条 · 第 2 / 2 页')).toBeVisible();
  await page.getByLabel('搜索风险', { exact: true }).fill('P001');
  await page.getByLabel('风险状态', { exact: true }).selectOption('open');
  await expect(page.getByLabel('搜索风险', { exact: true })).toHaveValue('P001');
  await expect(page.locator('tbody tr')).toHaveCount(1);
  await expect(page).toHaveURL(/q=P001/);
  const listUrl = page.url();
  await page.getByRole('link', { name: '查看事件' }).click();
  await page.goBack();
  await expect(page).toHaveURL(listUrl);
  await expect(page.getByLabel('搜索风险', { exact: true })).toHaveValue('P001');
  await page.reload();
  await expect(page.getByLabel('风险状态', { exact: true })).toHaveValue('open');
});

test('所有设计入口和示例详情可直接打开', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  for (const path of ['/assistant', '/assistant/THREAD-001', '/risks/EVT-001?tab=plans', '/approvals', '/approvals/APR-001', '/data', '/data/connections', '/data/imports/new', '/data/imports/BATCH-001', '/data/catalog', '/data/catalog/inventory', '/data/governance?tab=mapping', '/data/governance?tab=lineage', '/data/governance/issues/ISS-012', '/data/preview/inventory', '/runs', '/runs/RUN-008', '/settings?tab=monitoring']) {
    await page.goto(path);
    if (path === '/data' || path === '/data/connections') await expect(page.getByRole('link', { name: '下载数据模板' })).toBeVisible();
    else if (path === '/data/catalog') await expect(page.getByRole('table', { name: '数据集就绪状态' })).toBeVisible();
    else await expect(page.locator('.sp-frame h1')).toBeVisible();
    await expect(page.getByText('页面未找到', { exact: true })).toHaveCount(0);
  }
  expect(errors).toEqual([]);
  await page.goto('/risks/EVT-missing');
  await expect(page.getByRole('heading', { name: '对象不存在或不在当前工厂范围' })).toBeVisible();
  await page.goto('/risks/EVT-001?factory=B');
  await expect(page.getByRole('heading', { name: '对象不存在或不在当前工厂范围' })).toBeVisible();
  await page.goto('/missing');
  await expect(page.getByRole('heading', { name: '页面未找到' })).toBeVisible();
});

test('数据中心仅折叠菜单，旧地址跳转并保留工厂范围', async ({ page }) => {
  await page.route('**/api/data/**', route => route.fulfill({ json: [] }));
  await page.goto('/workspace?factory=B');
  const group = page.getByRole('button', { name: '数据中心', exact: true });
  await expect(group).toHaveAttribute('aria-expanded', 'false');
  await group.click();
  await expect(group).toHaveAttribute('aria-expanded', 'true');
  await expect(page).toHaveURL(/\/workspace\?factory=B$/);
  await group.press('Enter');
  await expect(group).toHaveAttribute('aria-expanded', 'false');
  await expect(page).toHaveURL(/\/workspace\?factory=B$/);
  await page.goto('/data?factory=B#records');
  await expect(page).toHaveURL(/\/data\/connections\?factory=B#records$/);
  await expect(page.getByRole('heading', { name: '数据接入', exact: true })).toHaveCount(0);
  await expect(page.getByText('业务数据来源', { exact: true })).toHaveCount(0);
  await expect(page.getByRole('link', { name: '下载数据模板' })).toBeVisible();
  await expect(group).toHaveAttribute('aria-expanded', 'true');
  await expect(page.getByRole('link', { name: '数据中心', exact: true })).toHaveCount(0);
  await group.click();
  await expect(page.locator('#data-subnav')).toHaveCount(0);
  await expect(page).toHaveURL(/\/data\/connections\?factory=B#records$/);
  await page.goBack();
  await expect(page).toHaveURL(/\/workspace\?factory=B$/);
  await group.click();
  for (const [title, path] of [['数据接入', '/data/connections'], ['数据目录', '/data/catalog'], ['数据治理', '/data/governance'], ['数据预览', '/data/preview/inventory']]) {
    await page.locator('#data-subnav').getByRole('link', { name: title, exact: true }).click();
    await expect(page).toHaveURL(path + '?factory=B');
    await expect(group).toHaveClass(/is-active/);
  }
  await group.click();
  await page.getByRole('link', { name: '返回目录', exact: true }).click();
  await expect(group).toHaveAttribute('aria-expanded', 'true');
});

test('模块首页标题移至顶栏，筛选同步且详情保留面包屑', async ({ page }) => {
  for (const [path, title] of [['/workspace', '工作台'], ['/risks', '风险处置'], ['/approvals', '审批中心'], ['/runs', '运行记录']]) {
    await page.goto(path);
    await expect(page.locator('.sp-topbar h1')).toHaveText(title);
    await expect(page.locator('main h1')).toHaveCount(0);
    await expect(page.locator('.sp-breadcrumb')).toHaveCount(0);
  }
  await page.goto('/risks');
  await expect(page.locator('.sp-topbar-heading p')).toContainText('确定性缺料计算');
  await page.getByLabel('搜索风险', { exact: true }).fill('P001');
  await expect(page.getByRole('table', { name: '风险事件列表' }).locator('tbody tr')).toHaveCount(1);
  await expect(page.getByRole('link', { name: '打开智能助手', exact: true })).toHaveCount(0);
  await page.getByRole('link', { name: '查看事件' }).click();
  await expect(page.locator('.sp-breadcrumb')).toBeVisible();
  await expect(page.locator('main h1')).toBeVisible();
  await page.getByRole('link', { name: '返回列表', exact: true }).click();
  await expect(page.locator('.sp-topbar h1')).toHaveText('风险处置');
  await expect(page.locator('h1')).toHaveCount(1);
});

test('历史对话更多操作支持键盘关闭和关联事件跳转', async ({ page }) => {
  await page.goto('/assistant');
  const more = page.getByLabel('P001 缺料调查的更多操作');
  await more.click();
  await expect(page.locator('.sp-conversation-menu')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.locator('.sp-conversation-menu')).not.toBeVisible();
  await expect(more).toBeFocused();
  await more.click();
  await page.locator('.sp-conversation-menu').getByRole('link', { name: '打开会话' }).click();
  await expect(page).toHaveURL(/assistant\/THREAD-001/);
  await expect(page.locator('.sp-conversation-menu')).not.toBeVisible();
  await more.click();
  await page.locator('.sp-conversation-menu').getByRole('link', { name: '查看关联事件' }).click();
  await expect(page).toHaveURL(/risks\/EVT-001/);
});

test('草稿刷新保留，切换工厂确认后范围隔离', async ({ page }) => {
  await page.goto('/assistant?factory=A');
  await page.getByLabel('供应链消息草稿').fill('检查未来三天缺料风险');
  await page.reload();
  await expect(page.getByLabel('供应链消息草稿')).toHaveValue('检查未来三天缺料风险');
  await expect(page.getByRole('button', { name: '发送', exact: true })).toBeDisabled();
  page.once('dialog', dialog => dialog.dismiss());
  await page.getByLabel('工厂范围').selectOption('B');
  await expect(page.getByLabel('工厂范围')).toHaveValue('A');
  page.once('dialog', dialog => dialog.accept());
  await page.getByLabel('工厂范围').selectOption('B');
  await expect(page.getByLabel('供应链消息草稿')).toHaveValue('');
});

test('手机布局、导航与表格不使页面横向溢出', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  for (const path of ['/workspace', '/risks', '/risks/EVT-001', '/data', '/assistant', '/settings', '/account']) {
    await page.goto(path);
    if (path === '/data') await expect(page.getByRole('link', { name: '下载数据模板' })).toBeVisible();
    else await expect(page.locator('.sp-frame h1')).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    if (path === '/workspace') await page.screenshot({ path: testInfo.outputPath('workspace-mobile.png'), fullPage: true });
    if (path === '/risks/EVT-001') await page.screenshot({ path: testInfo.outputPath('risk-mobile.png'), fullPage: true });
    if (path === '/account') await page.screenshot({ path: testInfo.outputPath('account-mobile.png'), fullPage: true });
  }
  await page.getByRole('button', { name: '打开导航' }).click();
  await expect(page.getByRole('link', { name: '工作台', exact: true })).toBeVisible();
  await page.getByRole('link', { name: '工作台', exact: true }).click();
  await expect(page).toHaveURL(/workspace/);
  await expect(page.getByRole('button', { name: '打开导航' })).toHaveAttribute('aria-expanded', 'false');
});

test('未登录深链接登录后回到原详情', async ({ page, context }) => {
  let signedIn = false;
  await context.route('**/api/auth/session', route => route.fulfill(signedIn ? { json: { user: { name: '测试计划员' } } } : { status: 401, json: { message: '未登录。' } }));
  await context.route('**/api/auth/login', route => { signedIn = true; return route.fulfill({ json: { user: { name: '测试计划员' } } }); });
  await page.goto('/risks/EVT-001?tab=plans&factory=A');
  await expect(page).toHaveURL(/login\?next=/);
  await page.locator('input').first().fill('test@example.com');
  await page.locator('input[type=password]').fill('test-password');
  await page.locator('button[type=submit]').click();
  await expect(page).toHaveURL(/\/risks\/EVT-001\?tab=plans&factory=A/);
  await expect(page.getByRole('button', { name: '方案比较', exact: true })).toHaveAttribute('aria-current', 'page');
});

test('只保留供应链导航，账号复用公共布局，退出后路由受保护', async ({ page, context }, testInfo) => {
  const analystCalls: string[] = [];
  page.on('request', request => { if (request.url().includes('/api/analyst')) analystCalls.push(request.url()); });
  await page.goto('/');
  await expect(page).toHaveURL(/\/workspace$/);
  await expect(page.getByRole('link', { name: '订单与支付分析', exact: true })).toHaveCount(0);
  await expect(page.locator('.sp-nav-item')).toHaveText(['工作台', '智能助手', '风险处置', '审批中心', '数据中心', '运行记录', '系统设置']);
  await page.locator('.sp-user a').click();
  await expect(page).toHaveURL(/account/);
  await expect(page.getByRole('heading', { name: '账号设置', exact: true })).toBeVisible();
  await expect(page.locator('.sp-sidebar')).toHaveCount(1);
  await expect(page.getByLabel('工厂范围')).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('account-desktop.png'), fullPage: true });
  await page.reload();
  await expect(page.getByRole('heading', { name: '账号设置', exact: true })).toBeVisible();
  await page.getByRole('link', { name: '返回工作台', exact: true }).click();
  await page.goto('/order-analysis');
  await expect(page.getByRole('heading', { name: '页面未找到' })).toBeVisible();
  expect(analystCalls).toEqual([]);
  await page.getByRole('link', { name: '返回工作台', exact: true }).click();
  await context.route('**/api/auth/logout', route => route.fulfill({ json: { ok: true } }));
  await page.getByRole('button', { name: '退出登录', exact: true }).click();
  await expect(page).toHaveURL(/login-select/);
  await page.goBack();
  await expect(page).toHaveURL(/login\?next=/);
});

test('普通登录默认进入供应链工作台，注册和找回密码入口可达', async ({ page, context }) => {
  let signedIn = false;
  await context.route('**/api/auth/session', route => route.fulfill(signedIn ? { json: { user: { name: '测试计划员' } } } : { status: 401, json: { message: '未登录。' } }));
  await context.route('**/api/auth/login', route => { signedIn = true; return route.fulfill({ json: { user: { name: '测试计划员' } } }); });
  for (const path of ['/login-select', '/register', '/forgot-password']) {
    await page.goto(path);
    await expect(page.locator('form, h1').first()).toBeVisible();
  }
  await page.goto('/login');
  await page.locator('input').first().fill('test@example.com');
  await page.locator('input[type=password]').fill('test-password');
  await page.locator('button[type=submit]').click();
  await expect(page).toHaveURL(/\/workspace$/);
});
